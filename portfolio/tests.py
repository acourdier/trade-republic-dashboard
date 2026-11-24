from datetime import date, timedelta

from portfolio import analytics as a
from portfolio.analytics.benchmarks import replay
from portfolio.analytics.xirr import xirr
from portfolio.models import Kind


def row(day, kind, amount, isin="", name="X"):
    return a.Row(day=day, kind=kind, amount=abs(amount), label=name, isin=isin,
                 name=name)


D = date(2025, 1, 1)


def test_xirr_simple_year():
    assert xirr([(D, -100.0), (D + timedelta(days=365), 110.0)]) == 10.0
    assert xirr([(D, -100.0), (D + timedelta(days=10), -5.0)]) is None


def test_cash_flow_formulas():
    rows = [row(D, Kind.DEPOSIT, 1000), row(D, Kind.TAX_REFUND, 50),
            row(D, Kind.WITHDRAWAL, -200), row(D, Kind.CARD, -100),
            row(D, Kind.BUY, -600), row(D, Kind.SELL, 100)]
    cf = a.cash_flow(rows, current_value=1000.0, income_total=20.0)
    assert cf["net_capital_in"] == 850  # 1000 + 50 − 200, card NOT subtracted
    assert cf["lifetime_pl"] == 1000 + 100 - 850 - 20
    assert cf["net_traded"] == 500
    assert cf["monthly"][0]["net_flow"] == 1000 + 50 - 100 - 200


def test_lifetime_pl_none_when_history_incomplete():
    cf = a.cash_flow([row(D, Kind.WITHDRAWAL, -10)], current_value=5.0, income_total=0)
    assert cf["lifetime_pl"] is None


def test_forward_dividends_needs_90_days():
    on = date(2026, 1, 1)
    few = [row(on - timedelta(days=d), Kind.DIVIDEND, 10) for d in (10, 40)]
    assert a.forward_dividends(few, on)[0] is None
    half_year = [row(on - timedelta(days=d), Kind.DIVIDEND, 10) for d in (0, 182)]
    assert a.forward_dividends(half_year, on)[0] == round(20 * 365 / 182, 2)


def test_cost_basis_and_replay():
    rows = [row(D, Kind.BUY, -100), row(D + timedelta(days=2), Kind.SELL, 50)]
    cb = a.cost_basis_series(rows, D + timedelta(days=3))
    assert cb == [(D, 100.0), (D + timedelta(days=2), 50.0), (D + timedelta(days=3), 50.0)]
    bench = {D: 10.0, D + timedelta(days=2): 20.0}
    out = dict(replay(cb, bench, D + timedelta(days=3)))
    # 10 units bought at 10; on day 2 sell €50 at 20 → 7.5 units × 20
    assert out[D] == 100.0 and out[D + timedelta(days=3)] == 150.0


def test_allocation_includes_cash():
    pos = [{"asset_class": "ETFs", "country": "IE", "value": 300.0},
           {"asset_class": "Stocks", "country": "US", "value": 100.0}]
    al = a.allocation(pos, cash=100.0)
    assert al["by_class"][0] == {"label": "ETFs", "value": 300.0, "pct": 60.0}
    assert {c["label"] for c in al["by_country"]} == {"IE", "US"}


# ---------------------------------------------------------------------------
# Value history rebuild
# ---------------------------------------------------------------------------
from portfolio.analytics import ecb  # noqa: E402
from portfolio.analytics import history as h  # noqa: E402
from portfolio.analytics import projection as pj  # noqa: E402

W = "IE00B4L5Y983"
G = "IE00B579F325"


def ev(day, kind, amount, isin="", **kw):
    return h.Event(day=day, kind=kind, amount=amount, isin=isin, **kw)


def by_day(days):
    return {p.day: p for p in days}


def test_rebuild_exact_shares_times_prices():
    events = [ev(D, Kind.DEPOSIT, 1000.0),
              ev(D, Kind.BUY, -501.0, W, shares=5.0, price=100.0, fees=1.0)]
    closes = {W: [(D, 100.0), (D + timedelta(days=2), 110.0)]}
    days, rep = h.rebuild_series(events, closes, [], D + timedelta(days=3))
    out = by_day(days)
    assert (out[D].cash, out[D].depot) == (499.0, 500.0)
    assert out[D + timedelta(days=3)].depot == 550.0  # carried over the gap
    assert (out[D].net_capital_in, out[D].cost_basis) == (1000.0, 501.0)
    assert (rep.exact, rep.estimated, rep.priced) == (1, 0, 1)


def test_rebuild_estimates_inconsistent_or_missing_shares():
    events = [ev(D, Kind.BUY, -100.0, W, shares=999.0, price=1.0),  # doesn't add up
              ev(D + timedelta(days=1), Kind.BUY, -60.0, W)]
    closes = {W: [(D, 10.0), (D + timedelta(days=1), 20.0)]}
    days, rep = h.rebuild_series(events, closes, [], D + timedelta(days=1))
    assert by_day(days)[D + timedelta(days=1)].depot == (10 + 3) * 20.0
    assert (rep.exact, rep.estimated) == (0, 2)


def test_rebuild_holds_unpriced_instruments_at_cost():
    events = [ev(D, Kind.BUY, -300.0, G), ev(D + timedelta(days=1), Kind.SELL, 100.0, G)]
    days, rep = h.rebuild_series(events, {}, [], D + timedelta(days=1), {G: "Private Equity"})
    assert [p.depot for p in days] == [300.0, 200.0]
    assert rep.at_cost == ["Private Equity"]


def test_rebuild_reconciles_with_latest_snapshot():
    d1, d2 = D + timedelta(days=1), D + timedelta(days=2)
    events = [ev(D, Kind.DEPOSIT, 1000.0),
              ev(D, Kind.BUY, -100.0, W), ev(D, Kind.BUY, -100.0, G),
              ev(d1, Kind.SELL, 90.0, G)]  # net of tax: estimate leaves a remainder
    closes = {W: [(D, 10.0)], G: [(D, 10.0)]}
    snap = h.Snapshot(day=d2, cash=885.0, depot=120.0, quantities={W: 12.0})
    days, rep = h.rebuild_series(events, closes, [snap], d2)
    out = by_day(days)
    assert out[D].depot == 12 * 10.0 + 100.0  # W scaled to TR's 12 shares
    assert out[d1].depot == 120.0  # G sold out → zero from its last sale
    assert (out[d2].cash, out[d2].depot, out[d2].source) == (885.0, 120.0, "snapshot")
    assert (rep.cash_gap, rep.depot_gap) == (5.0, 0.0)  # 1000 − 200 + 90 = 890
    assert rep.scaled == [{"isin": W, "name": W, "factor": 1.2}]


def test_rebuild_is_anchored_on_snapshots():
    events = [ev(D, Kind.DEPOSIT, 100.0)]
    d = [D + timedelta(days=i) for i in range(6)]
    snaps = [h.Snapshot(day=d[2], cash=110.0, depot=0.0, quantities={}),
             h.Snapshot(day=d[4], cash=90.0, depot=0.0, quantities={})]
    days, _ = h.rebuild_series(events, {}, snaps, d[5])
    # ramp 0 → +10 from the first event, then +10 → −10, then −10 carried
    assert [p.cash for p in days] == [100.0, 105.0, 110.0, 100.0, 90.0, 90.0]
    assert [p.source for p in days][2::2] == ["snapshot", "snapshot"]


def test_rebuild_weekly_bars_and_noise_are_not_a_unit():
    mon = date(2025, 3, 3)  # weekly bars stamped Monday, closing Sunday
    end = date(2025, 3, 26)
    weekly = [(mon + timedelta(days=7 * i), c) for i, c in enumerate([100.0, 104.0, 96.0, 100.0])]
    assert h.period_ends(weekly, end)[-2:] == [(date(2025, 3, 23), 96.0), (end, 100.0)]
    # Thursday of the week whose bar closes at 96; read at its stamp, that bar
    # would make 102 / 96 look like a 6 % unit.
    trade = mon + timedelta(days=17)
    events = [ev(trade, Kind.BUY, -1020.0, W, shares=10.0, price=102.0)]
    days, _ = h.rebuild_series(events, {W: weekly}, [], end)
    assert by_day(days)[end].depot == 1000.0
    assert h.unit_scale([1.06, 1.063]) == 1.0
    assert h.unit_scale([0.0099, 0.0101]) == 0.01
    assert h.unit_scale([]) == 1.0


def test_rebuild_bond_quoted_in_percent():
    events = [ev(D, Kind.BUY, -990.0, W, shares=1000.0, price=99.0)]  # 1000 face at 99 %
    days, rep = h.rebuild_series(events, {W: [(D, 99.0), (D + timedelta(days=1), 100.0)]},
                                 [], D + timedelta(days=1))
    assert by_day(days)[D + timedelta(days=1)].depot == 1000.0
    assert rep.exact == 1


def test_ecb_csv_parsing():
    daily = ("KEY,TIME_PERIOD,OBS_VALUE\nX,2025-06-10,2.25\nX,2025-06-11,2\n"
             "X,2025-06-12,2\nX,bad,1\n")
    assert ecb.changes_only(ecb.parse_csv(daily)) == [(date(2025, 6, 10), 2.25),
                                                      (date(2025, 6, 11), 2.0)]
    monthly = "KEY,TIME_PERIOD,OBS_VALUE\nX,2025-06,129.1\n"
    assert ecb.parse_csv(monthly) == [(date(2025, 6, 1), 129.1)]


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def test_forward_contributions_and_growth():
    a = pj.Assumptions(monthly_contribution=800, contribution_growth_pct=10)
    out = pj.forward(0.0, 0.0, 0.0, date(2026, 11, 24), date(2028, 12, 1), 0.0, a, 0.0)
    assert out[0][0] == date(2026, 12, 1)
    assert out[11][1] == 12 * 800 and out[12][1] == 12 * 800 + 880
    out = pj.forward(1000.0, 500.0, 0.0, D, date(2026, 1, 1), 12.0, pj.Assumptions(0), 0.0)
    assert round(out[-1][1], 6) == round(1000 * 1.12 + 500, 6)


def test_replay_moves_the_sold_share_of_the_depot_to_cash():
    d1 = D + timedelta(days=1)
    events = [ev(D, Kind.DEPOSIT, 1000.0), ev(D, Kind.BUY, -400.0, W),
              ev(D, Kind.DIVIDEND, 5.0, W),  # part of the return, not replayed
              ev(d1, Kind.SELL, 300.0, W)]  # 50 % of a €600 depot
    out = pj.replay(0.0, events, {D: 600.0, d1: 300.0}, D, d1)
    assert out[0][1:] == (400.0, 600.0)
    assert out[1][1:] == (200.0, 800.0)


def test_implied_return_recovers_the_rate():
    end = D + timedelta(days=730)
    events = [ev(D, Kind.DEPOSIT, 1000.0), ev(D, Kind.BUY, -1000.0, W)]
    target = 1000 * 1.06 ** (730 / 365.25)
    assert pj.implied_return(events, {}, D, end, target) == 6.0


def test_deflator_hicp_then_assumption():
    hicp = [(date(2025, 1, 1), 100.0), (date(2026, 1, 1), 110.0)]
    defl = pj.Deflator(hicp, date(2026, 1, 15), 2.0)
    assert defl.to_today(date(2025, 3, 1), 100.0) == 110.0
    later = date(2027, 1, 31)
    assert round(defl.to_today(later, 102.0), 6) == round(102 / 1.02 ** (365 / 365.25), 6)


def test_clean_and_from_dict():
    data, errors = pj.clean({**{k: "1" for k in pj.FIELDS}, "cash_rate_pct": "",
                             "baseline_pct": "abc", "horizon_years": "100"})
    assert data["cash_rate_pct"] is None
    assert set(errors) == {"baseline_pct", "horizon_years"}
    a = pj.from_dict({"baseline_pct": "6", "optimistic_pct": None, "horizon_years": "x"})
    assert (a.baseline_pct, a.optimistic_pct, a.horizon_years) == (6.0, 7.0, 30)


def test_build_opening_and_today_bases():
    start, now = date(2023, 5, 31), date(2023, 7, 5)
    events = [ev(start, Kind.DEPOSIT, 1000.0), ev(start, Kind.BUY, -1000.0, W)]
    actual = [pj.Actual(start + timedelta(days=i), 0.0, 1000.0 + i, 1000.0)
              for i in range((now - start).days + 1)]
    a = pj.Assumptions(monthly_contribution=100, horizon_years=30)
    p = pj.build(a, events, actual, [], None)
    assert p["end"] == date(2053, 5, 31) and p["cash_rate"] == pj.FALLBACK_CASH_RATE
    base = next(ln for ln in p["lines"] if ln["key"] == "baseline")
    assert len(base["points"]) == len(p["paid_points"]) > len(p["actual_points"])
    assert base["points"][0][1] == 1000.0
    assert [m["label"] for m in p["milestones"]][:2] == ["2025", "2030"]
    assert p["milestones"][-1]["label"] == "May 2053"
    assert p["implied_return"] > 0
    t = pj.build(a, events, actual, [], 2.5, base="today", real=True)
    tl = next(ln for ln in t["lines"] if ln["key"] == "baseline")
    assert tl["points"][0][1] is None
    today_point = next(pt for pt in tl["points"] if pt[0] == now.isoformat())
    assert today_point[1] == actual[-1].total
    assert t["cash_rate"] == 2.5
    assert pj.build(a, events, [], [], None) is None
