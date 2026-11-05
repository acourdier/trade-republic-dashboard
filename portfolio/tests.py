from datetime import date, timedelta

from portfolio import analytics as a
from portfolio.analytics.benchmarks import replay
from portfolio.analytics.xirr import xirr
from portfolio.models import Kind


def row(day, kind, amount, isin="", name="X"):
    return a.Row(day=day, kind=kind, amount=abs(amount), signed=amount, label=name, isin=isin,
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
