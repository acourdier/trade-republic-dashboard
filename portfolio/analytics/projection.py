"""Projection from the account's opening over `horizon_years`, three scenarios.

Pure functions: the view feeds them the ValuePoint history, timeline events
and ECB data.

Up to today each scenario replays your own decisions: the same deposits,
withdrawals and card spending, the same buys, the same share of the depot
sold, and the interest TR actually paid on cash. Only the invested money
differs: it earns the scenario's annual return instead of what your positions
did. So "actual vs. scenario" today compares returns, not saving habits.

After today, each month the planned contribution is invested (raised by the
yearly increase every twelve months), invested money earns the scenario's
return and the cash left earns the cash rate (default: the latest ECB deposit
rate). With base="today" the future starts from your real cash and depot.

Real values are in today's euros: deflated with the euro-area HICP while it is
published, then with the assumed inflation.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, timedelta

from portfolio.analytics.history import Event
from portfolio.models import Kind

YEAR_DAYS = 365.25
FALLBACK_CASH_RATE = 2.0

SCENARIOS = [  # key, label, colour token (palette order: indigo, ochre, celadon)
    ("baseline", "Baseline", "--s1"),
    ("optimistic", "Optimistic", "--s2"),
    ("pessimistic", "Pessimistic", "--s3"),
]


@dataclass(frozen=True)
class Assumptions:
    monthly_contribution: float = 800.0
    contribution_growth_pct: float = 0.0
    pessimistic_pct: float = 2.0
    baseline_pct: float = 5.0
    optimistic_pct: float = 7.0
    inflation_pct: float = 2.0
    cash_rate_pct: float | None = None  # None: latest ECB deposit rate
    horizon_years: int = 30

    def rate(self, scenario: str) -> float:
        return getattr(self, f"{scenario}_pct")


# name → (label, min, max); the form and the validation both use it.
FIELDS: dict[str, tuple[str, float, float]] = {
    "monthly_contribution": ("Monthly contribution (€)", 0, 1_000_000),
    "contribution_growth_pct": ("Yearly increase (%)", -50, 50),
    "pessimistic_pct": ("Pessimistic return (% a year)", -50, 50),
    "baseline_pct": ("Baseline return (% a year)", -50, 50),
    "optimistic_pct": ("Optimistic return (% a year)", -50, 50),
    "inflation_pct": ("Inflation (% a year)", -20, 50),
    "cash_rate_pct": ("Cash rate (% a year)", -10, 50),
    "horizon_years": ("Horizon (years from opening)", 1, 60),
}


def from_dict(data: Mapping | None) -> Assumptions:
    """Stored settings → Assumptions; unknown or broken values fall back to defaults."""
    a = Assumptions()
    if not isinstance(data, Mapping):
        return a
    for f in fields(Assumptions):
        v = data.get(f.name)
        if v is None:
            continue
        try:
            a = replace(a, **{f.name: int(v) if f.name == "horizon_years" else float(v)})
        except (TypeError, ValueError):
            continue
    return a


def clean(post: Mapping[str, str]) -> tuple[dict, dict[str, str]]:
    """Form data → (settings dict, errors by field). Empty cash rate = ECB."""
    out: dict = {}
    errors: dict[str, str] = {}
    for name, (label, lo, hi) in FIELDS.items():
        raw = (post.get(name) or "").strip().replace(",", ".")
        if raw == "" and name == "cash_rate_pct":
            out[name] = None
            continue
        try:
            v = float(raw)
        except ValueError:
            errors[name] = f"{label}: enter a number."
            continue
        if not lo <= v <= hi:
            errors[name] = f"{label}: between {lo:g} and {hi:g}."
            continue
        out[name] = int(v) if name == "horizon_years" else v
    return out, errors


def as_dict(a: Assumptions) -> dict:
    return asdict(a)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def grid(start: date, today: date, end: date) -> list[date]:
    """Weekly up to today, today, then the first of every month to `end`."""
    out, d = [], start
    while d < today:
        out.append(d)
        d += timedelta(days=7)
    out.append(today)
    d = next_month(today)
    while d <= end:
        out.append(d)
        d = next_month(d)
    return out


# ---------------------------------------------------------------------------
# Inflation
# ---------------------------------------------------------------------------
class Deflator:
    """Price level per day: HICP (monthly, carried) then the assumed inflation."""

    def __init__(self, hicp: list[tuple[date, float]], today: date, inflation_pct: float):
        self.days = [d for d, _ in hicp]
        self.values = [v for _, v in hicp]
        self.growth = 1 + inflation_pct / 100
        self.today = today
        self.base = self.level(today)

    def level(self, d: date) -> float:
        if not self.days:
            return self.growth ** ((d - self.today).days / YEAR_DAYS)
        last_month_end = next_month(self.days[-1]) - timedelta(days=1)
        if d > last_month_end:
            return self.values[-1] * self.growth ** ((d - last_month_end).days / YEAR_DAYS)
        i = bisect.bisect_right(self.days, d) - 1
        return self.values[max(i, 0)]

    def to_today(self, d: date, value: float) -> float:
        return value * self.base / self.level(d)


# ---------------------------------------------------------------------------
# History replay and the future
# ---------------------------------------------------------------------------
_CASH_FLOW_KINDS = {Kind.DEPOSIT, Kind.TAX_REFUND, Kind.WITHDRAWAL, Kind.CARD, Kind.INTEREST}


def replay(rate_pct: float, events: list[Event], depot_by_day: Mapping[date, float],
           start: date, end: date) -> list[tuple[date, float, float]]:
    """[(day, invested, cash)] with the invested money earning `rate_pct` a year.

    Dividends and bond coupons/redemptions of your real positions are part of
    the scenario's return, so they are not replayed as cash; a redemption
    counts as a sale.
    """
    growth = (1 + rate_pct / 100) ** (1 / YEAR_DAYS)
    by_day: dict[date, list[Event]] = defaultdict(list)
    for e in events:
        by_day[e.day].append(e)
    inv = cash = prev_depot = 0.0
    out = []
    d = start
    while d <= end:
        inv *= growth
        for e in by_day.get(d, ()):
            if e.kind in _CASH_FLOW_KINDS:
                cash += e.amount
            elif e.kind == Kind.BUY:
                inv += abs(e.amount)
                cash -= abs(e.amount)
            elif e.kind in (Kind.SELL, Kind.BOND_REDEMPTION):
                p = abs(e.amount)
                moved = inv * min(1.0, p / prev_depot) if prev_depot > 0 else min(p, inv)
                inv -= moved
                cash += moved
        out.append((d, inv, cash))
        prev_depot = depot_by_day.get(d, prev_depot)
        d += timedelta(days=1)
    return out


def implied_return(events: list[Event], depot_by_day: Mapping[date, float], start: date,
                   end: date, target: float) -> float | None:
    """The annual return on invested money that reproduces today's real value."""
    def total(r: float) -> float:
        _, inv, cash = replay(r, events, depot_by_day, start, end)[-1]
        return inv + cash

    lo, hi = -90.0, 300.0
    if not total(lo) <= target <= total(hi):
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        if total(mid) < target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 2)


def forward(invested: float, cash: float, paid_in: float, today: date, end: date,
            rate_pct: float, a: Assumptions,
            cash_rate_pct: float) -> list[tuple[date, float, float]]:
    """[(first of month, value, money paid in)] from the month after today to `end`."""
    g = (1 + rate_pct / 100) ** (1 / 12)
    c = (1 + cash_rate_pct / 100) ** (1 / 12)
    out, d, k = [], next_month(today), 0
    while d <= end:
        contribution = a.monthly_contribution * (1 + a.contribution_growth_pct / 100) ** (k // 12)
        invested = invested * g + contribution
        cash *= c
        paid_in += contribution
        out.append((d, invested + cash, paid_in))
        d, k = next_month(d), k + 1
    return out


# ---------------------------------------------------------------------------
# Page bundle
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Actual:
    day: date
    cash: float
    depot: float
    net_capital_in: float

    @property
    def total(self) -> float:
        return self.cash + self.depot


def build(a: Assumptions, events: list[Event], actual: list[Actual],
          hicp: list[tuple[date, float]], ecb_rate: float | None,
          base: str = "opening", real: bool = False) -> dict | None:
    if not actual:
        return None
    start, now = actual[0].day, actual[-1].day
    end = add_years(start, a.horizon_years)
    cash_rate = a.cash_rate_pct if a.cash_rate_pct is not None else (
        ecb_rate if ecb_rate is not None else FALLBACK_CASH_RATE)
    by_day = {p.day: p for p in actual}
    depot_by_day = {p.day: p.depot for p in actual}
    today = actual[-1]
    dates = grid(start, now, end)
    past = [d for d in dates if d <= now]
    defl = Deflator(hicp, now, a.inflation_pct)
    conv: Callable[[date, float], float] = defl.to_today if real else (lambda d, v: v)

    actual_days = [p.day for p in actual]

    def at(d: date) -> Actual:
        return by_day.get(d) or actual[max(bisect.bisect_right(actual_days, d) - 1, 0)]

    paid_now = today.net_capital_in
    lines, finals, at_today = [], {}, {}
    paid_future: list[tuple[date, float]] = []
    for key, label, colour in SCENARIOS:
        rate = a.rate(key)
        rep = replay(rate, events, depot_by_day, start, now)
        rep_by_day = {d: inv + cash for d, inv, cash in rep}
        _, inv_now, cash_now = rep[-1]
        at_today[key] = inv_now + cash_now
        if base == "today":
            inv_now, cash_now = today.depot, today.cash
            hist = [None] * (len(past) - 1) + [today.total]
        else:
            hist = [rep_by_day[d] for d in past]
        fut = forward(inv_now, cash_now, paid_now, now, end, rate, a, cash_rate)
        paid_future = [(d, p) for d, _, p in fut]
        values = hist + [v for _, v, _ in fut]
        lines.append({"key": key, "label": label, "colour": colour, "rate": rate,
                      "values": values})
        finals[key] = values[-1]

    paid = [at(d).net_capital_in for d in past] + [p for _, p in paid_future]
    actual_values = [at(d).total for d in past]

    def pts(values: list[float | None]) -> list[list]:
        return [[d.isoformat(), None if v is None else round(conv(d, v), 2)]
                for d, v in zip(dates, values, strict=False)]

    milestones = []
    years = list(range((start.year // 5 + 1) * 5, end.year, 5))
    marks = [(str(y), date(y, 12, 31)) for y in years if date(y, 12, 31) > now]
    marks.append((end.strftime("%b %Y"), end))
    for label, when in marks:
        i = bisect.bisect_right(dates, when) - 1
        d = dates[i]
        milestones.append({
            "label": label,
            "paid_in": conv(d, paid[i]),
            **{line["key"]: conv(d, line["values"][i]) for line in lines},
        })

    implied = implied_return(events, depot_by_day, start, now, today.total)
    return {
        "start": start, "today": now, "end": end, "base": base, "real": real,
        "cash_rate": cash_rate,
        "cash_rate_is_ecb": a.cash_rate_pct is None and ecb_rate is not None,
        "actual_total": today.total,
        "paid_today": today.net_capital_in,
        "at_today": at_today,
        "vs_baseline": today.total - at_today["baseline"],
        "implied_return": implied,
        "finals": {k: conv(dates[-1], v) for k, v in finals.items()},
        "final_paid_in": conv(dates[-1], paid[-1]),
        "lines": [{**{k: v for k, v in line.items() if k != "values"},
                   "points": pts(line["values"])} for line in lines],
        "actual_points": pts(actual_values),
        "paid_points": pts(paid),
        "milestones": milestones,
    }
