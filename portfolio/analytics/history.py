"""Account value per day, rebuilt from the timeline — stored as ValuePoint.

    cash(d)   = Σ signed amounts of the cash-moving events up to d
    shares(d) = Σ shares bought − shares sold up to d, per instrument
    depot(d)  = Σ shares(d) × price(d)

Shares per trade come from timelineDetailV2 when shares × price agrees with
the cash amount; otherwise they are estimated as amount ÷ that day's close.
Prices are TR's closes (PricePoint), carried over weekends and gaps. TR's long
ranges come as weekly bars stamped at the start of the week, so a bar counts
from the end of its period (capped at today). Per instrument, the median ratio
of execution price to close picks a unit (1, or 0.01/100 for prices quoted in
% of face value); a ratio near 1 is market noise, not a unit. An instrument with no
prices at all is held at its net cost. Days with a PortfolioSnapshot keep TR's
own cash and depot value, and the rebuilt line is anchored on them: the gap
(TR's figure − rebuilt) is interpolated between snapshots, carried after the
last one and phased in linearly from the first event to the first one, so the
line has no jumps. The gap at the latest snapshot is reported.

Reconciliation with the latest snapshot:
  * held then: the share path is scaled so the count matches TR's (absorbs
    splits and estimate drift), when the factor is within 0.5–2;
  * not held then: the position is zero from its last sale on (a sale's cash
    amount is net of tax and fees, so an estimate would leave a remainder).
"""

from __future__ import annotations

import bisect
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db import transaction as db_tx
from django.utils import timezone

from portfolio.models import (
    CASH_KINDS,
    Instrument,
    Kind,
    PortfolioSnapshot,
    PricePoint,
    Transaction,
    ValuePoint,
)

TOLERANCE = 0.03  # shares × price vs cash amount, relative
SCALE_LIMITS = (0.5, 2.0)
UNITS = (1.0, 0.01, 100.0)


@dataclass(frozen=True, slots=True)
class Event:
    day: date
    kind: str
    amount: float  # signed, as TR reports it (buys negative)
    isin: str = ""
    shares: float | None = None
    price: float | None = None
    fees: float = 0.0
    taxes: float = 0.0

    @property
    def gross(self) -> float:
        """Trade value before fees and taxes."""
        a = abs(self.amount)
        return a - self.fees if self.kind == Kind.BUY else a + self.fees + self.taxes


@dataclass(frozen=True, slots=True)
class Snapshot:
    day: date
    cash: float
    depot: float
    quantities: dict[str, float]


@dataclass(slots=True)
class Day:
    day: date
    cash: float
    depot: float
    net_capital_in: float
    cost_basis: float
    source: str = ValuePoint.Source.REBUILT


@dataclass(slots=True)
class Report:
    instruments: int = 0
    priced: int = 0
    at_cost: list[str] = field(default_factory=list)
    trades: int = 0
    exact: int = 0
    estimated: int = 0
    scaled: list[dict] = field(default_factory=list)
    cash_gap: float | None = None
    depot_gap: float | None = None
    checked_on: str | None = None


class Closes:
    """Price lookup with carry-forward; before the first close, the first close."""

    def __init__(self, points: list[tuple[date, float]]):
        self.days = [d for d, _ in points]
        self.values = [v for _, v in points]

    def __bool__(self) -> bool:
        return bool(self.days)

    def on(self, day: date) -> float:
        i = bisect.bisect_right(self.days, day) - 1
        return self.values[max(i, 0)]


def period_ends(points: list[tuple[date, float]], end: date) -> list[tuple[date, float]]:
    """Re-date bars stamped at the start of a period (weekly) to the period's end."""
    if len(points) < 3:
        return points
    gaps = [(b[0] - a[0]).days for a, b in zip(points, points[1:], strict=False)]
    spacing = statistics.median(gaps)
    if spacing < 2:  # daily closes
        return points
    shift = timedelta(days=round(spacing) - 1)
    return [(min(d + shift, end), v) for d, v in points]


def unit_scale(ratios: list[float]) -> float:
    """Execution price ÷ close → 1, 0.01 or 100; anything else is noise or unknown."""
    if not ratios:
        return 1.0
    m = statistics.median(ratios)
    return next((u for u in UNITS if 0.75 <= m / u <= 1.33), 1.0)


def exact_shares(ev: Event) -> float | None:
    """TR's share count when it is consistent with the cash amount."""
    if not ev.shares or not ev.price or ev.gross <= 0:
        return None
    for unit in (1.0, 0.01):  # bonds are quoted in % of face value
        if abs(ev.shares * ev.price * unit - ev.gross) <= TOLERANCE * ev.gross + 0.05:
            return ev.shares
    return None


def _steps(changes: list[tuple[date, float]]) -> tuple[list[date], list[float]]:
    """[(day, delta)] → cumulative step function."""
    days, values, running = [], [], 0.0
    for d, delta in sorted(changes, key=lambda c: c[0]):
        running += delta
        if days and days[-1] == d:
            values[-1] = running
        else:
            days.append(d)
            values.append(running)
    return days, values


def _at(days: list[date], values: list[float], day: date) -> float:
    i = bisect.bisect_right(days, day) - 1
    return values[i] if i >= 0 else 0.0


def rebuild_series(events: list[Event], closes: dict[str, list[tuple[date, float]]],
                   snapshots: list[Snapshot], end: date,
                   names: dict[str, str] | None = None) -> tuple[list[Day], Report]:
    names = names or {}
    rep = Report()
    if not events:
        return [], rep
    events = sorted(events, key=lambda e: e.day)
    trades: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if e.kind in (Kind.BUY, Kind.SELL) and e.isin:
            trades[e.isin].append(e)
    last_snap = max(snapshots, key=lambda s: s.day) if snapshots else None

    # Per instrument: a share step function (or a cost one) and a price lookup.
    holdings: dict[str, tuple[list[date], list[float], Closes | None, float]] = {}
    for isin, evs in trades.items():
        rep.instruments += 1
        rep.trades += len(evs)
        px = Closes(period_ends(closes.get(isin) or [], end))
        sign = {Kind.BUY: 1.0, Kind.SELL: -1.0}
        if not px:
            rep.at_cost.append(names.get(isin) or isin)
            changes = [(e.day, sign[e.kind] * e.gross) for e in evs]
        else:
            rep.priced += 1
            ratios = [(e.gross / s) / px.on(e.day) for e in evs if (s := exact_shares(e))]
            scale = unit_scale(ratios)
            changes = []
            for e in evs:
                s = exact_shares(e)
                if s is None:
                    s = e.gross / (px.on(e.day) * scale)
                    rep.estimated += 1
                else:
                    rep.exact += 1
                changes.append((e.day, sign[e.kind] * s))
        days, values = _steps(changes)
        values = [max(v, 0.0) for v in values]
        if last_snap is not None and evs[-1].day <= last_snap.day:
            held = last_snap.quantities.get(isin)
            if held is None and evs[-1].kind == Kind.SELL:
                values[-1] = 0.0
            elif held and px:
                rebuilt = _at(days, values, last_snap.day)
                factor = held / rebuilt if rebuilt > 0 else None
                if factor and SCALE_LIMITS[0] <= factor <= SCALE_LIMITS[1]:
                    values = [v * factor for v in values]
                if factor is None or abs(factor - 1) > 0.02:
                    rep.scaled.append({"isin": isin, "name": names.get(isin) or isin,
                                       "factor": round(factor, 3) if factor else None})
        holdings[isin] = (days, values, px or None, scale if px else 1.0)

    by_day: dict[date, list[Event]] = defaultdict(list)
    for e in events:
        by_day[e.day].append(e)
    snap_by_day = {s.day: s for s in snapshots}

    out: list[Day] = []
    gaps: list[tuple[int, float, float]] = []  # (index into out, cash gap, depot gap)
    cash = nci = cb = 0.0
    day = events[0].day
    while day <= end:
        for e in by_day.get(day, ()):
            if e.kind in CASH_KINDS:
                cash += e.amount
            if e.kind in (Kind.DEPOSIT, Kind.TAX_REFUND):
                nci += abs(e.amount)
            elif e.kind == Kind.WITHDRAWAL:
                nci -= abs(e.amount)
            elif e.kind == Kind.BUY:
                cb += abs(e.amount)
            elif e.kind == Kind.SELL:
                cb -= abs(e.amount)
        depot = 0.0
        for days, values, px, scale in holdings.values():
            q = _at(days, values, day)
            if q:
                depot += q * px.on(day) * scale if px else q
        point = Day(day, round(cash, 2), round(depot, 2), round(nci, 2), round(cb, 2))
        if (snap := snap_by_day.get(day)) is not None:
            gaps.append((len(out), snap.cash - point.cash, snap.depot - point.depot))
            if snap is last_snap:
                rep.cash_gap = round(point.cash - snap.cash, 2)
                rep.depot_gap = round(point.depot - snap.depot, 2)
                rep.checked_on = day.isoformat()
            point.cash, point.depot = snap.cash, snap.depot
            point.source = ValuePoint.Source.SNAPSHOT
        out.append(point)
        day += timedelta(days=1)
    _anchor(out, gaps)
    return out, rep


def _anchor(out: list[Day], gaps: list[tuple[int, float, float]]) -> None:
    """Shift rebuilt days by the snapshot gaps, interpolated between snapshots."""
    if not gaps:
        return
    knots = [(0, 0.0, 0.0), *gaps] if gaps[0][0] > 0 else gaps
    for (i0, c0, d0), (i1, c1, d1) in zip(knots, knots[1:], strict=False):
        for i in range(i0 + 1, i1):
            f = (i - i0) / (i1 - i0)
            out[i].cash = round(out[i].cash + c0 + (c1 - c0) * f, 2)
            out[i].depot = round(out[i].depot + d0 + (d1 - d0) * f, 2)
    last, c, d = gaps[-1]
    for p in out[last + 1:]:
        p.cash, p.depot = round(p.cash + c, 2), round(p.depot + d, 2)


# ---------------------------------------------------------------------------
# DB wrapper
# ---------------------------------------------------------------------------
def _f(v) -> float | None:
    return float(v) if v is not None else None


def load_events() -> list[Event]:
    qs = (Transaction.objects.filter(kind__in=CASH_KINDS).exclude(amount=None)
          .order_by("timestamp")
          .values_list("timestamp", "kind", "amount", "instrument_id", "shares", "price",
                       "fees", "taxes"))
    return [Event(day=timezone.localdate(ts), kind=kind, amount=float(amount), isin=isin or "",
                  shares=_f(shares), price=_f(price), fees=_f(fees) or 0.0,
                  taxes=_f(taxes) or 0.0)
            for ts, kind, amount, isin, shares, price, fees, taxes in qs]


def load_closes() -> dict[str, list[tuple[date, float]]]:
    out: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for series, d, v in (PricePoint.objects.exclude(series__startswith="ECB:")
                         .order_by("series", "date").values_list("series", "date", "value")):
        out[series].append((d, float(v)))
    return dict(out)


def load_snapshots() -> list[Snapshot]:
    """The last snapshot of each day."""
    by_day: dict[date, PortfolioSnapshot] = {}
    for s in PortfolioSnapshot.objects.order_by("taken_at").prefetch_related("positions"):
        by_day[timezone.localdate(s.taken_at)] = s
    return [Snapshot(day=d, cash=float(s.cash), depot=float(s.depot_value),
                     quantities={p.instrument_id: float(p.quantity) for p in s.positions.all()})
            for d, s in sorted(by_day.items())]


def rebuild(end: date | None = None) -> dict:
    """Recompute every ValuePoint from stored data (no network). Returns the report."""
    from dataclasses import asdict

    from broker.models import Setting

    names = dict(Instrument.objects.values_list("isin", "name"))
    days, rep = rebuild_series(load_events(), load_closes(), load_snapshots(),
                               end or timezone.localdate(), names)
    with db_tx.atomic():
        ValuePoint.objects.all().delete()
        ValuePoint.objects.bulk_create(
            [ValuePoint(date=p.day, cash=p.cash, depot_value=p.depot,
                        net_capital_in=p.net_capital_in, cost_basis=p.cost_basis,
                        source=p.source) for p in days],
            batch_size=500,
        )
    report = {**asdict(rep), "days": len(days), "built_at": timezone.now().isoformat()}
    Setting.put("history_report", report)
    return report
