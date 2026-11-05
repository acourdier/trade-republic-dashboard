from decimal import Decimal

import pytest

from broker import mapping
from broker.sync import build_transactions, shape_positions
from portfolio.models import Kind


@pytest.mark.parametrize(("ev", "kind"), [
    ({"eventType": "BANK_TRANSACTION_INCOMING"}, Kind.DEPOSIT),
    ({"eventType": "BANK_TRANSACTION_OUTGOING"}, Kind.WITHDRAWAL),
    ({"eventType": "CARD_TRANSACTION"}, Kind.CARD),
    ({"eventType": "TRADING_SAVINGSPLAN_EXECUTED"}, Kind.BUY),
    ({"eventType": "TRADING_TRADE_EXECUTED", "amount": {"value": -10.0}}, Kind.BUY),
    ({"eventType": "TRADING_TRADE_EXECUTED", "amount": {"value": 10.0}}, Kind.SELL),
    ({"eventType": "SSP_CORPORATE_ACTION_CASH", "title": "Apple"}, Kind.DIVIDEND),
    ({"eventType": "SSP_CORPORATE_ACTION_CASH_NON_DIVIDEND", "title": "Aug 2040",
      "subtitle": "Zinszahlung"}, Kind.INTEREST),
    ({"eventType": "SSP_CORPORATE_ACTION_CASH", "subtitle": "Endgültige Fälligkeit"},
     Kind.BOND_REDEMPTION),
    ({"eventType": "INTEREST_PAYOUT"}, Kind.INTEREST),
    ({"eventType": "SOMETHING_NEW"}, Kind.UNKNOWN),
])
def test_classify(ev, kind):
    assert mapping.classify(ev) == kind


def test_extract_isin_from_icon_and_fields():
    assert mapping.extract_isin({"icon": "logos/IE00B4L5Y983/v2"}) == "IE00B4L5Y983"
    assert mapping.extract_isin({"details": {"isin": "US0378331005"}}) == "US0378331005"
    assert mapping.extract_isin({"icon": "logos/bank/v2"}) == ""


def test_event_id_prefers_tr_id_and_is_stable_otherwise():
    assert mapping.event_id({"id": "abc"}) == "abc"
    ev = {"eventType": "X", "timestamp": "2026-01-01T00:00:00Z", "amount": {"value": 1}}
    assert mapping.event_id(ev) == mapping.event_id(dict(ev))
    assert mapping.event_id(ev).startswith("h:")


def test_parse_timestamp_variants():
    assert mapping.parse_timestamp("2026-11-02T09:12:44.393+0000").year == 2026
    assert mapping.parse_timestamp("2026-11-02T09:12:44Z").tzinfo is not None
    assert mapping.parse_timestamp("garbage") is None


def test_shape_positions_falls_back_and_folds_others():
    snap = {
        "portfolio": {"positions": [
            {"isin": "US26614N1028", "name": "DuPont", "netSize": "2", "averageBuyIn": "50",
             "currentPrice": "60", "exchangeIds": ["LSX"]},
            {"isin": "IE00B4L5Y983", "name": "Core MSCI World", "netSize": "1",
             "averageBuyIn": "80", "netValue": "100", "instrumentType": "fund"},
            {"isin": "XF000BTC0017", "netSize": "0", "averageBuyIn": "1"},  # sold out
        ]},
        "categories": {"US26614N1028": "others", "IE00B4L5Y983": "stocksAndETFs"},
    }
    out = shape_positions(snap)
    assert [p["isin"] for p in out] == ["US26614N1028", "IE00B4L5Y983"]
    assert out[0]["category"] == "stocksAndETFs"
    assert out[0]["value"] == Decimal("120.00")
    assert out[1]["price"] == Decimal("100") and out[1]["is_etf"] is True


@pytest.mark.django_db
def test_build_transactions_dedupes_and_creates_instruments():
    ev = {"id": "e1", "eventType": "TRADING_SAVINGSPLAN_EXECUTED", "title": "Core MSCI World",
          "timestamp": "2026-01-02T10:00:00Z", "amount": {"value": -50.0, "currency": "EUR"},
          "icon": "logos/IE00B4L5Y983/v2"}
    rows = build_transactions([("transactions", ev), ("activity", ev)])
    assert len(rows) == 1
    t = rows[0]
    assert (t.kind, t.amount, t.instrument_id) == (Kind.BUY, Decimal("-50.0"), "IE00B4L5Y983")
