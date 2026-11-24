"""One sync = login (if needed) → portfolio snapshot → timeline → DB."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction as db_tx
from django.utils import timezone

from broker import mapping, tr
from broker.models import SyncRun
from portfolio.models import (
    Instrument,
    Kind,
    PortfolioSnapshot,
    PositionSnapshot,
    PricePoint,
    Transaction,
)

log = logging.getLogger("trdash.sync")

INCREMENTAL_OVERLAP = timedelta(days=3)
_ETF_HINTS = ("etf", "ucits", "msci", "nasdaq", "s&p 500", "ishares", "vanguard", "xtrackers",
              "amundi", "spdr", "lyxor", "stoxx", "ftse all")
_TX_UPDATE_FIELDS = ["source", "timestamp", "kind", "amount", "currency", "instrument", "title",
                     "subtitle", "status", "event_type", "event_subtype", "raw"]


def _set(run: SyncRun, **fields) -> None:
    for k, v in fields.items():
        setattr(run, k, v)
    run.save(update_fields=list(fields))


def run_sync(run: SyncRun) -> None:
    """Execute a queued SyncRun. Never raises: failures land on the run."""
    _set(run, status=SyncRun.Status.RUNNING, started_at=timezone.now(), stage="Checking session")
    try:
        creds = tr.require_credentials()
        client = tr.authenticated_client(
            creds,
            on_awaiting_approval=lambda: _set(
                run,
                status=SyncRun.Status.AWAITING_APPROVAL,
                stage="Approve the login in your Trade Republic app",
            ),
        )
        _set(run, status=SyncRun.Status.RUNNING, stage="Reading portfolio")
        if run.mode == SyncRun.Mode.DOCUMENTS:
            counts = sync_documents(client, creds.phone, run)
        else:
            counts = sync_portfolio(client, run)
            _set(run, stage="Reading transactions")
            counts |= sync_transactions(client, full=run.mode == SyncRun.Mode.FULL)
            _set(run, stage="Updating benchmarks")
            counts["benchmarks"] = refresh_benchmarks()
            counts["history"] = sync_history(
                client, full=run.mode == SyncRun.Mode.FULL,
                stage=lambda text: _set(run, stage=text))
        _set(run, status=SyncRun.Status.DONE, stage="Done", counts=counts,
             finished_at=timezone.now(), message="")
        log.info("sync #%s done: %s", run.pk, counts)
    except tr.TrError as e:
        log.warning("sync #%s failed (%s): %s", run.pk, e.code, e)
        _set(run, status=SyncRun.Status.FAILED, error_code=e.code, message=str(e),
             finished_at=timezone.now())
    except Exception as e:  # defensive: never leave a run stuck as "running"
        log.exception("sync #%s crashed", run.pk)
        _set(run, status=SyncRun.Status.FAILED, error_code="internal", message=repr(e)[:500],
             finished_at=timezone.now())


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
def _dec(v: Any) -> Decimal:
    return mapping.to_decimal(v) or Decimal(0)


def _cash_eur(cash: Any) -> Decimal:
    """The cash payload is sometimes a dict, sometimes a list of accounts."""
    if isinstance(cash, dict):
        return _dec(cash.get("amount"))
    if isinstance(cash, list):
        for entry in cash:
            if isinstance(entry, dict) and entry.get("currencyId") in ("EUR", "EUR_CASH", None):
                return _dec(entry.get("amount"))
        if cash and isinstance(cash[0], dict):
            return _dec(cash[0].get("amount"))
    return Decimal(0)


def _is_etf(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _ETF_HINTS)


def shape_positions(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """tr-api snapshot_full → [{isin, name, category, quantity, avg_cost, price, value}]."""
    categories: dict[str, str] = snap.get("categories") or {}
    out = []
    for raw in (snap.get("portfolio") or {}).get("positions") or []:
        qty = _dec(raw.get("netSize") or raw.get("virtualSize") or raw.get("quantity"))
        if qty <= 0:
            continue
        avg = _dec(raw.get("averageBuyIn") or raw.get("avgPrice"))
        cp = raw.get("currentPrice")
        price = _dec(cp.get("value") or cp.get("price")) if isinstance(cp, dict) else _dec(cp)
        value = _dec(raw.get("netValue") or raw.get("currentValue"))
        if value <= 0 and price > 0:
            value = price * qty
        if price <= 0 and value > 0:
            price = value / qty
        instrument_id = str(raw.get("instrumentId") or raw.get("isin") or "")
        isin = str(raw.get("isin") or instrument_id.split(".", 1)[0])
        if not isin:
            continue
        category = categories.get(isin, "others")
        # TR mis-tags plain equities as "others" — fold into Stocks & ETFs.
        if category == "others":
            category = Instrument.Category.STOCKS_ETFS
        exchanges = raw.get("exchangeIds") or []
        out.append({
            "isin": isin,
            "name": (raw.get("name") or raw.get("instrumentName") or "").strip() or isin,
            "is_etf": str(raw.get("instrumentType") or "").lower() == "fund" or None,
            "category": category,
            "exchange": exchanges[0] if exchanges and isinstance(exchanges[0], str) else "",
            "quantity": qty,
            "avg_cost": avg,
            "price": price if price > 0 else None,
            "value": value.quantize(Decimal("0.01")),
        })
    return out


def sync_portfolio(client, run: SyncRun | None) -> dict[str, int]:
    snap = tr.fetch_portfolio(client)
    positions = shape_positions(snap)
    with db_tx.atomic():
        for p in positions:
            Instrument.objects.update_or_create(
                isin=p["isin"],
                defaults={"name": p["name"], "category": p["category"],
                          "exchange": p["exchange"],
                          "is_etf": p["is_etf"] if p["is_etf"] is not None else _is_etf(p["name"])},
            )
        ps = PortfolioSnapshot.objects.create(
            taken_at=timezone.now(),
            sync_run=run,
            cash=_cash_eur(snap.get("cash")).quantize(Decimal("0.01")),
            depot_value=sum((p["value"] for p in positions), Decimal(0)),
            depot_cost=sum((p["quantity"] * p["avg_cost"] for p in positions), Decimal(0))
            .quantize(Decimal("0.01")),
            raw=snap,
        )
        PositionSnapshot.objects.bulk_create(
            PositionSnapshot(snapshot=ps, instrument_id=p["isin"], quantity=p["quantity"],
                             avg_cost=p["avg_cost"], price=p["price"], value=p["value"])
            for p in positions
        )
    return {"positions": len(positions), "portfolio_topics": snap.get("diagnostics", {})}


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
_NAMED_KINDS = {Kind.BUY, Kind.SELL, Kind.DIVIDEND, Kind.INTEREST, Kind.BOND_REDEMPTION}


def _incremental_cutoff():
    last = (SyncRun.objects.filter(status=SyncRun.Status.DONE)
            .exclude(mode=SyncRun.Mode.DOCUMENTS).order_by("-finished_at").first())
    if last is None or not Transaction.objects.exists():
        return None
    return last.finished_at - INCREMENTAL_OVERLAP


def build_transactions(items: list[tuple[str, dict[str, Any]]]) -> list[Transaction]:
    """[(source, raw event)] → unsaved Transactions; creates missing Instruments."""
    known = set(Instrument.objects.values_list("isin", flat=True))
    rows: dict[str, Transaction] = {}
    for source, ev in items:
        f = mapping.transaction_fields(ev)
        if f["timestamp"] is None:
            continue
        isin = f.pop("isin")
        if isin and isin not in known:
            name = f["title"] if f["kind"] in _NAMED_KINDS else ""
            Instrument.objects.get_or_create(isin=isin, defaults={"name": name,
                                                                   "is_etf": _is_etf(name)})
            known.add(isin)
        tid = mapping.event_id(ev)
        rows[tid] = Transaction(tr_id=tid, source=source, instrument_id=isin or None, raw=ev, **f)
    return list(rows.values())


def sync_transactions(client, *, full: bool) -> dict[str, int]:
    cutoff = None if full else _incremental_cutoff()
    tx_items, act_items = tr.fetch_timeline(client, cutoff=cutoff)
    items = [("transactions", e) for e in tx_items] + [("activity", e) for e in act_items]
    with db_tx.atomic():
        rows = build_transactions(items)
        Transaction.objects.bulk_create(
            rows, batch_size=500, update_conflicts=True, unique_fields=["tr_id"],
            update_fields=_TX_UPDATE_FIELDS,
        )
        removed = 0
        if cutoff is None and rows:  # full refresh: drop events TR no longer returns
            removed, _ = Transaction.objects.exclude(tr_id__in=[r.tr_id for r in rows]).delete()
    return {"timeline_transactions": len(tx_items), "timeline_activity": len(act_items),
            "stored": len(rows), "removed": removed, "incremental": cutoff is not None}


def refresh_benchmarks() -> dict[str, int]:
    """Best effort — a Yahoo outage must not fail the sync."""
    from portfolio.analytics import benchmarks

    first = (Transaction.objects.filter(kind=Kind.BUY).order_by("timestamp")
             .values_list("timestamp", flat=True).first())
    if first is None:
        return {}
    try:
        return benchmarks.refresh(timezone.localdate(first))
    except Exception as e:
        log.info("benchmark refresh failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Value history: trade details, price history, ECB data, then the rebuild
# ---------------------------------------------------------------------------
PRICES_REFRESH_AFTER = timedelta(hours=20)
_TRADES = [Kind.BUY, Kind.SELL]


def sync_details(client, *, full: bool) -> dict[str, int]:
    """timelineDetailV2 for trades without one (a full sync retries failures)."""
    trades = Transaction.objects.filter(kind__in=_TRADES).exclude(tr_id__startswith="h:")
    pending = trades.filter(detail__isnull=True)
    if full:
        pending = pending | trades.filter(detail__has_key="error")
    ids = list(pending.values_list("tr_id", flat=True))
    if not ids:
        return {"fetched": 0, "with_shares": 0}
    got = tr.fetch_trade_details(client, ids)
    rows = []
    for t in Transaction.objects.filter(tr_id__in=ids):
        t.detail = got.get(t.tr_id) or {"error": "no answer"}
        for k, v in mapping.parse_detail(t.detail).items():
            setattr(t, k, v)
        rows.append(t)
    Transaction.objects.bulk_update(rows, ["detail", "shares", "price", "fees", "taxes"],
                                    batch_size=500)
    return {"fetched": len(ids), "with_shares": sum(1 for t in rows if t.shares is not None)}


def sync_prices(client, *, force: bool) -> dict[str, int]:
    """TR closes for every instrument ever traded, at most once per 20 h."""
    from broker.models import Setting

    last = Setting.get("prices_fetched")
    if not force and last and timezone.now() - datetime.fromisoformat(last) < PRICES_REFRESH_AFTER:
        return {}
    isins = set(Transaction.objects.filter(kind__in=_TRADES).exclude(instrument=None)
                .values_list("instrument_id", flat=True))
    exchanges = dict(Instrument.objects.filter(isin__in=isins).values_list("isin", "exchange"))
    got = tr.fetch_price_history(client, exchanges)
    with db_tx.atomic():
        for isin, ex in got["exchanges"].items():
            if ex and not exchanges.get(isin):
                Instrument.objects.filter(isin=isin).update(exchange=ex[:20])
        PricePoint.objects.bulk_create(
            [PricePoint(series=isin, date=day, value=Decimal(f"{close:.6f}"))
             for isin, points in got["closes"].items() for day, close in points],
            batch_size=500, update_conflicts=True, unique_fields=["series", "date"],
            update_fields=["value"],
        )
    Setting.put("prices_fetched", timezone.now().isoformat())
    return {"instruments": len(exchanges), "priced": len(got["closes"])}


def sync_history(client, *, full: bool, stage=lambda text: None) -> dict[str, Any]:
    """Best effort, like the benchmarks: a refusal from TR must not fail the sync."""
    from portfolio.analytics import ecb, history

    out: dict[str, Any] = {}
    steps = [
        ("details", "Reading trade details", lambda: sync_details(client, full=full)),
        ("prices", "Reading price history", lambda: sync_prices(client, force=full)),
        ("ecb", "Reading ECB rates", lambda: ecb.refresh(_first_event_day())),
    ]
    for key, text, fn in steps:
        stage(text)
        try:
            out[key] = fn()
        except Exception as e:
            log.info("%s failed: %s", key, e)
            out[key] = {"error": str(e)[:200]}
    stage("Rebuilding value history")
    try:
        rep = history.rebuild()
        out["rebuilt_days"] = rep["days"]
    except Exception as e:
        log.exception("history rebuild failed")
        out["rebuild_error"] = repr(e)[:200]
    return out


def _first_event_day():
    first = Transaction.objects.order_by("timestamp").values_list("timestamp", flat=True).first()
    return timezone.localdate(first) if first else timezone.localdate()


def sync_documents(client, phone: str, run: SyncRun) -> dict[str, int]:
    from django.conf import settings

    _set(run, stage="Downloading documents (this can take a while)")
    report = tr.download_documents(client, settings.DOCUMENTS_DIR, phone)
    from portfolio.documents import index_documents

    indexed = index_documents()
    counts = getattr(report, "counts", None) or {}
    return {**{k: v for k, v in counts.items() if isinstance(v, int)}, "indexed": indexed}


def reclassify() -> int:
    """Re-run the mapping over stored raw payloads and trade details (no network)."""
    changed = 0
    for t in Transaction.objects.iterator():
        f = mapping.transaction_fields(t.raw)
        if t.detail is not None:
            f |= mapping.parse_detail(t.detail)
        isin = f.pop("isin")
        new = {k: v for k, v in f.items() if k != "timestamp" and getattr(t, k) != v}
        if isin and t.instrument_id != isin and Instrument.objects.filter(isin=isin).exists():
            new["instrument_id"] = isin
        if new:
            for k, v in new.items():
                setattr(t, k, v)
            t.save(update_fields=[*new.keys()])
            changed += 1
    return changed
