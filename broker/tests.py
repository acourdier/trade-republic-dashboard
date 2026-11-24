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
    ({"eventType": "SAVINGS_PLAN_INVOICE_CREATED", "subtitle": "Saving executed"}, Kind.BUY),
    ({"eventType": "CARD_ATM_WITHDRAWAL"}, Kind.CARD),
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


@pytest.mark.parametrize(("text", "value"), [
    ("€1,234.56", 1234.56),
    ("1.234,56 €", 1234.56),
    ("0.394738", 0.394738),
    ("0,394738", 0.394738),
    ("12,5", 12.5),
    ("1,234", 1234.0),
    ("\u2212€3.50", -3.5),
    ("1.234.567", 1234567.0),
    ("Free", None),
    (None, None),
    (2.5, 2.5),
])
def test_parse_number(text, value):
    assert mapping.parse_number(text) == value


# Shape after TR's detail page: a table section, the trade one level deeper
# behind an action payload, rows repeated on the nested page.
DETAIL_EN = {"id": "e1", "sections": [
    {"type": "header", "title": "You invested €50.00"},
    {"type": "table", "title": "Overview", "data": [
        {"title": "Status", "detail": {"text": "Executed", "type": "status"}},
        {"title": "Transaction", "detail": {"text": "0.394738 × €126.66", "action": {
            "type": "infoPage", "payload": {"sections": [{"type": "table", "data": [
                {"title": "Shares", "detail": {"text": "0.394738"}},
                {"title": "Share price", "detail": {"text": "€126.66"}},
                {"title": "Fee", "detail": {"text": "Free"}},
                {"title": "Total", "detail": {"text": "€50.00"}},
            ]}]}}}},
    ]},
    {"type": "table", "title": "Transaction", "data": [
        {"title": "Shares", "detail": {"text": "0.394738"}},
    ]},
]}
DETAIL_DE = {"sections": [{"type": "table", "title": "Transaktion", "data": [
    {"title": "Anteile", "detail": {"text": "12,5"}},
    {"title": "Aktienkurs", "detail": {"text": "80,00 €"}},
    {"title": "Gebühr", "detail": {"text": "1,00 €"}},
    {"title": "Kapitalertragsteuer", "detail": {"text": "20,00 €"}},
    {"title": "Solidaritätszuschlag", "detail": {"text": "1,10 €"}},
]}]}


def test_parse_detail_english_nested():
    d = mapping.parse_detail(DETAIL_EN)
    assert d["shares"] == Decimal("0.39473800")
    assert d["price"] == Decimal("126.66000000")
    assert d["fees"] is None and d["taxes"] is None


def test_parse_detail_german_sums_taxes():
    d = mapping.parse_detail(DETAIL_DE)
    assert (d["shares"], d["price"]) == (Decimal("12.50000000"), Decimal("80.00000000"))
    assert d["fees"] == Decimal("1.0000") and d["taxes"] == Decimal("21.1000")


def test_parse_detail_error_and_garbage():
    assert set(mapping.parse_detail({"error": "no answer"}).values()) == {None}
    assert set(mapping.parse_detail("nope").values()) == {None}


def test_parse_aggregates():
    payload = {"aggregates": [
        {"time": 1700006400000, "close": "10.5"},
        {"time": 1699920000000, "close": 10.0},
        {"time": 1700092800000, "close": None},
    ]}
    assert mapping.parse_aggregates(payload) == [("2023-11-14", 10.0), ("2023-11-15", 10.5)]
    assert mapping.parse_aggregates(None) == []


def _trade(tr_id, amount, isin="IE00B4L5Y983", day="2026-01-02"):
    from portfolio.models import Instrument, Transaction

    Instrument.objects.get_or_create(isin=isin)
    return Transaction.objects.create(
        tr_id=tr_id, source="transactions", timestamp=f"{day}T10:00:00Z",
        kind=Kind.BUY if amount < 0 else Kind.SELL, amount=Decimal(str(amount)),
        instrument_id=isin)


@pytest.mark.django_db
def test_sync_details_stores_parsed_fields_and_retries_on_full(monkeypatch):
    from broker import sync, tr
    from portfolio.models import Transaction

    _trade("t1", -50)
    _trade("t2", -20)
    calls = []

    def fake(client, ids):
        calls.append(sorted(ids))
        return {"t1": DETAIL_EN, "t2": {"error": "no answer"}}

    monkeypatch.setattr(tr, "fetch_trade_details", fake)
    assert sync.sync_details(None, full=False) == {"fetched": 2, "with_shares": 1}
    assert Transaction.objects.get(tr_id="t1").shares == Decimal("0.39473800")
    assert sync.sync_details(None, full=False) == {"fetched": 0, "with_shares": 0}
    sync.sync_details(None, full=True)
    assert calls == [["t1", "t2"], ["t2"]]


@pytest.mark.django_db
def test_sync_history_is_best_effort(monkeypatch):
    from broker import sync, tr
    from portfolio.analytics import ecb
    from portfolio.models import ValuePoint

    _trade("t1", -50)

    def refuse(*a, **k):
        raise tr.TrError("unknown topic")

    monkeypatch.setattr(tr, "fetch_trade_details", refuse)
    monkeypatch.setattr(tr, "fetch_price_history", refuse)
    monkeypatch.setattr(ecb, "refresh", lambda start: {})
    stages = []
    out = sync.sync_history(None, full=True, stage=stages.append)
    assert out["details"] == {"error": "unknown topic"}
    assert out["prices"] == {"error": "unknown topic"}
    assert out["rebuilt_days"] == ValuePoint.objects.count() > 0
    assert stages[-1] == "Rebuilding value history"


@pytest.mark.django_db
def test_sync_prices_stores_closes_and_exchanges(monkeypatch):
    from broker import sync, tr
    from portfolio.models import Instrument, PricePoint

    _trade("t1", -50)
    seen = {}

    def fake(client, exchanges):
        seen.update(exchanges)
        return {"closes": {"IE00B4L5Y983": [("2026-01-02", 100.0)]},
                "exchanges": {"IE00B4L5Y983": "LSX"}}

    monkeypatch.setattr(tr, "fetch_price_history", fake)
    assert sync.sync_prices(None, force=False) == {"instruments": 1, "priced": 1}
    assert seen == {"IE00B4L5Y983": ""}
    assert Instrument.objects.get(isin="IE00B4L5Y983").exchange == "LSX"
    assert PricePoint.objects.get(series="IE00B4L5Y983").value == Decimal("100")
    assert sync.sync_prices(None, force=False) == {}  # fetched recently


def test_batch_in_chunks_stops_when_tr_never_answers():
    import asyncio

    from broker import tr

    class FakeWS:
        calls = 0

        async def batch_fetch(self, payloads, timeout):
            FakeWS.calls += 1
            return [None] * len(payloads)

    payloads = [{"type": "timelineDetailV2", "id": str(i)} for i in range(120)]
    out = asyncio.run(tr._batch_in_chunks(FakeWS(), payloads, 50))
    assert len(out) == 120 and FakeWS.calls == 1
