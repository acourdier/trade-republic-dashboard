"""Portfolio analytics — pure computation over Transactions and snapshots.

Formulas are carried over from the legacy analyze_analytics.py:

    net_capital_in = deposits + tax_refunds − withdrawals
        Card spending is NOT subtracted: it's lifestyle consumption, not
        capital leaving the "committed to investing" pool.
    lifetime_pl = current_value + card_spending − net_capital_in − income
        Price appreciation on committed capital, excluding lifestyle spending
        and dividend/interest receipts (returns ON capital, not capital).
    xirr over deposits (−), withdrawals (+) and today's value (+).
    cost basis history = cumulative (buys − sells) per day.

One deliberate change: yield on cost divides the forward 12-month dividend
estimate by the cost of the positions held today (the legacy code divided by
lifetime buys, which understates it).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.cache import cache
from django.utils import timezone

from broker.models import SyncRun
from portfolio.analytics import benchmarks
from portfolio.analytics.xirr import xirr
from portfolio.models import Kind, PortfolioSnapshot, Transaction

FLOW_KINDS = {
    Kind.DEPOSIT: "deposits",
    Kind.TAX_REFUND: "tax_refunds",
    Kind.CARD: "card",
    Kind.WITHDRAWAL: "withdrawals",
    Kind.BUY: "buys",
    Kind.SELL: "sells",
}


@dataclass(frozen=True, slots=True)
class Row:
    day: date
    kind: str
    amount: float  # absolute value, EUR
    signed: float
    label: str
    isin: str
    name: str


def today() -> date:
    return timezone.localdate()


def load_rows() -> list[Row]:
    rows = []
    qs = (Transaction.objects.exclude(amount=None)
          .select_related("instrument").order_by("timestamp"))
    for t in qs:
        a = float(t.amount)
        rows.append(Row(
            day=timezone.localdate(t.timestamp),
            kind=t.kind,
            amount=abs(a),
            signed=a,
            label=t.label,
            isin=t.instrument_id or "",
            name=(t.instrument.name if t.instrument and t.instrument.name else t.title),
        ))
    return rows


# ---------------------------------------------------------------------------
# Positions (latest snapshot)
# ---------------------------------------------------------------------------
def latest_snapshot() -> PortfolioSnapshot | None:
    return PortfolioSnapshot.objects.order_by("-taken_at").first()


def positions(snapshot: PortfolioSnapshot | None) -> list[dict]:
    if snapshot is None:
        return []
    out = []
    depot = float(snapshot.depot_value) or 1.0
    for p in snapshot.positions.select_related("instrument"):
        cost = float(p.quantity * p.avg_cost)
        value = float(p.value)
        pl = value - cost if value > 0 else 0.0
        out.append({
            "isin": p.instrument_id,
            "name": p.instrument.name or p.instrument_id,
            "asset_class": p.instrument.asset_class,
            "country": p.instrument.country,
            "quantity": float(p.quantity),
            "avg_cost": float(p.avg_cost),
            "price": float(p.price) if p.price is not None else None,
            "value": value,
            "cost": cost,
            "pl": pl,
            "pl_pct": (pl / cost * 100) if cost and value > 0 else None,
            "weight": value / depot * 100,
        })
    out.sort(key=lambda x: x["value"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Cash flow
# ---------------------------------------------------------------------------
def cash_flow(rows: list[Row], current_value: float, income_total: float) -> dict:
    totals = {k: {"count": 0, "total": 0.0} for k in FLOW_KINDS.values()}
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(FLOW_KINDS.values(),
                                                                             0.0))
    for r in rows:
        key = FLOW_KINDS.get(r.kind)
        if key is None:
            continue
        totals[key]["count"] += 1
        totals[key]["total"] += r.amount
        monthly[r.day.strftime("%Y-%m")][key] += r.amount

    t = {k: v["total"] for k, v in totals.items()}
    net_capital_in = t["deposits"] + t["tax_refunds"] - t["withdrawals"]
    if net_capital_in > 0:
        lifetime_pl = current_value + t["card"] - net_capital_in - income_total
        lifetime_pl_pct = lifetime_pl / net_capital_in * 100
    else:  # history incomplete — don't show a misleading number
        lifetime_pl = lifetime_pl_pct = None

    months = []
    for m in sorted(monthly):
        d = monthly[m]
        months.append({
            "month": m,
            **{k: round(v, 2) for k, v in d.items()},
            "net_flow": round(d["deposits"] + d["tax_refunds"] - d["card"] - d["withdrawals"], 2),
            "net_invested": round(d["buys"] - d["sells"], 2),
        })
    return {
        "totals": totals,
        "net_capital_in": net_capital_in,
        "net_traded": t["buys"] - t["sells"],
        "current_value": current_value,
        "lifetime_pl": lifetime_pl,
        "lifetime_pl_pct": lifetime_pl_pct,
        "monthly": months,
    }


def xirr_pct(rows: list[Row], current_value: float, on: date) -> float | None:
    flows = [(r.day, -r.amount) for r in rows if r.kind == Kind.DEPOSIT]
    flows += [(r.day, r.amount) for r in rows if r.kind == Kind.WITHDRAWAL]
    if current_value > 0:
        flows.append((on, current_value))
    return xirr(flows)


# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------
def forward_dividends(payments: list[Row], on: date) -> tuple[float | None, int, int]:
    """Scale the last 365 days of dividends to a year. None with < 90 days of data."""
    cutoff = on - timedelta(days=365)
    relevant = [p for p in payments if p.day >= cutoff and p.kind == Kind.DIVIDEND]
    if not relevant:
        return None, 0, 0
    span = max(1, (max(p.day for p in relevant) - min(p.day for p in relevant)).days)
    if span < 90:
        return None, span, len(relevant)
    total = sum(p.amount for p in relevant)
    return round(total * 365.0 / span if span < 365 else total, 2), span, len(relevant)


def income(rows: list[Row], on: date, depot_cost: float) -> dict:
    payments = [r for r in rows if r.kind in (Kind.DIVIDEND, Kind.INTEREST)]
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: {"dividend": 0.0, "interest": 0.0})
    by_issuer: dict[str, dict] = {}
    for p in payments:
        monthly[p.day.strftime("%Y-%m")][p.kind] += p.amount
        key = p.isin or p.name
        b = by_issuer.setdefault(key, {"name": p.name, "isin": p.isin, "count": 0, "total": 0.0,
                                       "first": p.day, "last": p.day, "kind": p.kind})
        b["count"] += 1
        b["total"] += p.amount
        b["first"], b["last"] = min(b["first"], p.day), max(b["last"], p.day)
    fwd, basis_days, used = forward_dividends(payments, on)
    year_ago = on - timedelta(days=365)
    return {
        "payments": sorted(payments, key=lambda p: p.day, reverse=True),
        "monthly": [{"month": m, **v} for m, v in sorted(monthly.items())],
        "by_issuer": sorted(by_issuer.values(), key=lambda b: b["total"], reverse=True),
        "total": sum(p.amount for p in payments),
        "dividends_total": sum(p.amount for p in payments if p.kind == Kind.DIVIDEND),
        "interest_total": sum(p.amount for p in payments if p.kind == Kind.INTEREST),
        "last_12m": sum(p.amount for p in payments if p.day >= year_ago),
        "forward_12m": fwd,
        "forward_basis_days": basis_days,
        "forward_payments": used,
        "yield_on_cost": (fwd / depot_cost * 100) if fwd and depot_cost > 0 else None,
    }


# ---------------------------------------------------------------------------
# Histories
# ---------------------------------------------------------------------------
def cost_basis_series(rows: list[Row], on: date) -> list[tuple[date, float]]:
    """Cumulative (buys − sells) per day with a trade — committed capital."""
    running, by_day = 0.0, {}
    for r in rows:
        if r.kind == Kind.BUY:
            running += r.amount
        elif r.kind == Kind.SELL:
            running -= r.amount
        else:
            continue
        by_day[r.day] = round(running, 2)
    series = sorted(by_day.items())
    if series and series[-1][0] != on:
        series.append((on, series[-1][1]))
    return series


def net_worth_series() -> list[tuple[date, float]]:
    """Real total value per day, from the end-of-day PortfolioSnapshot."""
    by_day: dict[date, float] = {}
    for taken_at, cash, depot in (PortfolioSnapshot.objects.order_by("taken_at")
                                  .values_list("taken_at", "cash", "depot_value")):
        by_day[timezone.localdate(taken_at)] = float(cash + depot)
    return sorted(by_day.items())


def benchmark_series(cost_basis: list[tuple[date, float]], on: date) -> list[dict]:
    out = []
    for symbol, label in benchmarks.BENCHMARKS:
        replayed = benchmarks.replay(cost_basis, benchmarks.closes(symbol), on)
        if replayed:
            out.append({"symbol": symbol, "label": label, "series": replayed})
    return out


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
def allocation(pos: list[dict], cash: float) -> dict:
    by_class: dict[str, float] = defaultdict(float)
    by_country: dict[str, float] = defaultdict(float)
    for p in pos:
        by_class[p["asset_class"]] += p["value"]
        by_country[p["country"]] += p["value"]
    if cash > 0:
        by_class["Cash"] += cash
    total = sum(by_class.values()) or 1.0
    depot = sum(by_country.values()) or 1.0
    return {
        "by_class": [{"label": k, "value": v, "pct": v / total * 100}
                     for k, v in sorted(by_class.items(), key=lambda x: -x[1])],
        "by_country": [{"label": k, "value": v, "pct": v / depot * 100}
                       for k, v in sorted(by_country.items(), key=lambda x: -x[1])],
    }


# ---------------------------------------------------------------------------
# Page-level bundles (cached until the next sync)
# ---------------------------------------------------------------------------
def _cache_key(name: str) -> str:
    """Changes whenever a sync, snapshot, new event or benchmark price lands."""
    from portfolio.models import BenchmarkPrice

    parts = (
        SyncRun.objects.filter(status=SyncRun.Status.DONE).values_list("pk", flat=True).first(),
        PortfolioSnapshot.objects.values_list("pk", flat=True).first(),
        Transaction.objects.count(),
        BenchmarkPrice.objects.count(),
        today(),
    )
    return f"analytics:{name}:" + ":".join(map(str, parts))


def overview() -> dict:
    key = _cache_key("overview")
    if (hit := cache.get(key)) is not None:
        return hit
    on = today()
    snap = latest_snapshot()
    pos = positions(snap)
    rows = load_rows()
    cash = float(snap.cash) if snap else 0.0
    depot_value = float(snap.depot_value) if snap else 0.0
    depot_cost = float(snap.depot_cost) if snap else 0.0
    current_value = cash + depot_value
    inc = income(rows, on, depot_cost)
    cf = cash_flow(rows, current_value, inc["total"])
    cb = cost_basis_series(rows, on)
    data = {
        "snapshot": snap,
        "today": on,
        "cash": cash,
        "depot_value": depot_value,
        "depot_cost": depot_cost,
        "depot_pl": depot_value - depot_cost,
        "depot_pl_pct": ((depot_value - depot_cost) / depot_cost * 100) if depot_cost else None,
        "total": current_value,
        "positions": pos,
        "cash_flow": cf,
        "income": inc,
        "xirr": xirr_pct(rows, current_value, on),
        "cost_basis": cb,
        "net_worth": net_worth_series(),
        "benchmarks": benchmark_series(cb, on),
        "allocation": allocation(pos, cash),
        "contributors": {
            "top": sorted([p for p in pos if p["pl"] > 0], key=lambda p: -p["pl"])[:5],
            "bottom": sorted([p for p in pos if p["pl"] < 0], key=lambda p: p["pl"])[:5],
        },
    }
    cache.set(key, data, 3600)
    return data


def position_detail(isin: str) -> dict:
    """Everything about one instrument: trades, income, value over snapshots."""
    txs = list(Transaction.objects.filter(instrument_id=isin).order_by("-timestamp"))
    snaps = list(
        PortfolioSnapshot.objects.filter(positions__instrument_id=isin)
        .order_by("taken_at")
        .values_list("taken_at", "positions__value", "positions__quantity",
                     "positions__avg_cost", "positions__price")
    )
    buys = sum(abs(float(t.amount)) for t in txs if t.kind == Kind.BUY and t.amount is not None)
    sells = sum(abs(float(t.amount)) for t in txs if t.kind == Kind.SELL and t.amount is not None)
    inc = sum(abs(float(t.amount)) for t in txs
              if t.kind in (Kind.DIVIDEND, Kind.INTEREST) and t.amount is not None)
    history: dict[date, tuple] = {}
    for taken_at, value, qty, avg, price in snaps:
        history[timezone.localdate(taken_at)] = (float(value), float(qty), float(avg),
                                                 float(price) if price is not None else None)
    current = None
    snap = latest_snapshot()
    if snap is not None:
        current = next((p for p in positions(snap) if p["isin"] == isin), None)
    # Money-weighted view across the whole holding period, realised + unrealised.
    value_now = current["value"] if current else 0.0
    total_return = value_now + sells + inc - buys
    return {
        "transactions": txs,
        "current": current,
        "bought": buys,
        "sold": sells,
        "income": inc,
        "total_return": total_return,
        "total_return_pct": (total_return / buys * 100) if buys else None,
        "history": sorted(history.items()),
        "first_trade": min((t.timestamp for t in txs if t.kind == Kind.BUY), default=None),
        "trade_count": sum(1 for t in txs if t.kind in (Kind.BUY, Kind.SELL)),
    }

