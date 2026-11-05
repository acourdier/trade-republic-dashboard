"""Trade Republic timeline event → Transaction fields.

Ported from the legacy tr_fetch.py (EVENT_TYPE_MAP and its classifiers), which
was verified against real TR data in 2026. Pure functions, no I/O, so
`manage.py reclassify` can re-run them over stored raw payloads.

TR renamed most eventTypes during 2026; the old names stay under "legacy" so
older payloads still classify.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from portfolio.models import Kind

TRADE = "trade"  # sentinel: decided by amount sign
CORPORATE_ACTION = "corporate_action"  # sentinel: decided by title/subtitle

EVENT_TYPE_MAP: dict[str, str] = {
    # Cash in
    "BANK_TRANSACTION_INCOMING": Kind.DEPOSIT,
    "CARD_REFUND": Kind.DEPOSIT,
    "CRYPTO_TRANSACTION_INCOMING": Kind.DEPOSIT,
    "PAYMENT_INBOUND_CREDIT_CARD": Kind.DEPOSIT,
    "GIFTER_TRANSACTION": Kind.DEPOSIT,  # gift from another TR user
    "STOCK_PERK_REFUNDED": Kind.DEPOSIT,  # TR promo cash-back
    # legacy
    "INCOMING_TRANSFER": Kind.DEPOSIT,
    "INCOMING_TRANSFER_DELEGATION": Kind.DEPOSIT,
    "PAYMENT_INBOUND": Kind.DEPOSIT,
    "PAYMENT_INBOUND_SEPA_DIRECT_DEBIT": Kind.DEPOSIT,
    "card_refund": Kind.DEPOSIT,
    # Card spending: consumption, money leaves the user's wealth
    "CARD_TRANSACTION": Kind.CARD,
    "CRYPTO_TRANSFER_NETWORK_FEE": Kind.CARD,
    "card_successful_transaction": Kind.CARD,  # legacy
    # Withdrawals: money back to the user's own bank (still their money)
    "BANK_TRANSACTION_OUTGOING": Kind.WITHDRAWAL,
    "BANK_TRANSACTION_OUTGOING_DIRECT_DEBIT": Kind.WITHDRAWAL,
    "BANK_TRANSACTION_OUTGOING_SCHEDULED": Kind.WITHDRAWAL,
    "CRYPTO_TRANSACTION_OUTGOING": Kind.WITHDRAWAL,
    "SSP_SECURITIES_TRANSFER_OUTGOING": Kind.WITHDRAWAL,
    # legacy
    "OUTGOING_TRANSFER": Kind.WITHDRAWAL,
    "OUTGOING_TRANSFER_DELEGATION": Kind.WITHDRAWAL,
    "PAYMENT_OUTBOUND": Kind.WITHDRAWAL,
    # Tax
    "SSP_TAX_CORRECTION": Kind.TAX_REFUND,
    "ssp_tax_correction_invoice": Kind.TAX_REFUND,  # legacy
    "TAX_REFUND": Kind.TAX_REFUND,  # legacy
    # Trading — savings plan / round-up / saveback are always buys
    "TRADING_SAVINGSPLAN_EXECUTED": Kind.BUY,
    "SAVINGS_PLAN_EXECUTED": Kind.BUY,  # legacy
    "SPARE_CHANGE_AGGREGATE": Kind.BUY,
    "SAVEBACK_AGGREGATE": Kind.BUY,
    "TRADING_TRADE_EXECUTED": TRADE,
    "PRIVATE_MARKET_FUND_TRADE_EXECUTED": TRADE,
    "TRADE_INVOICE": TRADE,  # legacy
    "ORDER_EXECUTED": TRADE,  # legacy
    # Income — one eventType for dividends, bond coupons and bond maturities
    "SSP_CORPORATE_ACTION_CASH": CORPORATE_ACTION,
    "SSP_CORPORATE_ACTION_CASH_NON_DIVIDEND": CORPORATE_ACTION,
    "CREDIT": CORPORATE_ACTION,  # legacy
    "DIVIDEND": CORPORATE_ACTION,  # legacy
    "ssp_corporate_action_invoice_cash": CORPORATE_ACTION,  # legacy
    "INTEREST_PAYOUT": Kind.INTEREST,
    "INTEREST_PAYOUT_CREATED": Kind.INTEREST,
    # Order lifecycle (no cash)
    "ORDER_CANCELED": Kind.CANCELLED,
    "TRADING_ORDER_CANCELLED": Kind.CANCELLED,
    "ORDER_EXPIRED": Kind.EXPIRED,
    "TRADING_ORDER_EXPIRED": Kind.EXPIRED,
    "TRADING_ORDER_REJECTED": Kind.REJECTED,
    "TRADING_ORDER_CREATED": Kind.PENDING,
    # Intentionally unmapped (→ UNKNOWN, kept visible): CARD_VERIFICATION,
    # TRADING_SAVINGSPLAN_EXECUTION_FAILED, SSP_CORPORATE_ACTION_NO_CASH,
    # informational activity-log events.
}

_REDEMPTION_WORDS = ("endfälligkeit", "endgültige fälligkeit", "fälligkeit", "maturity",
                     "redemption", "principal return", "ausbuchung")
_COUPON_WORDS = ("zinszahlung", "coupon", "interest payment", "zinsgutschrift", "kuponzahlung")


def classify_corporate_action(title: str, subtitle: str) -> str:
    blob = f"{title} {subtitle}".lower()
    if any(w in blob for w in _REDEMPTION_WORDS):
        return Kind.BOND_REDEMPTION
    if any(w in blob for w in _COUPON_WORDS):
        return Kind.INTEREST
    return Kind.DIVIDEND


def classify_trade(ev: dict[str, Any]) -> str:
    val = _amount_value(ev)
    if isinstance(val, (int, float)):
        return Kind.BUY if val < 0 else Kind.SELL
    title = (ev.get("title") or "").lower()
    if "buy" in title or "kauf" in title:
        return Kind.BUY
    if "sell" in title or "verk" in title:
        return Kind.SELL
    return Kind.UNKNOWN


def classify(ev: dict[str, Any]) -> str:
    mapped = EVENT_TYPE_MAP.get(ev.get("eventType") or "")
    if mapped == TRADE:
        return classify_trade(ev)
    if mapped == CORPORATE_ACTION:
        return classify_corporate_action(
            (ev.get("title") or "").strip(), (ev.get("subtitle") or "").strip()
        )
    return mapped or Kind.UNKNOWN


def _looks_like_isin(s: str) -> bool:
    return len(s) == 12 and s[:2].isalpha() and s[2:].isalnum() and s.upper() == s


def extract_isin(ev: dict[str, Any]) -> str:
    """Best-effort ISIN: icon URL (logos/<ISIN>/v2), then direct and nested fields."""
    icon = ev.get("icon") or ""
    if "logos/" in icon:
        for piece in icon.split("/"):
            if _looks_like_isin(piece):
                return piece
    for key in ("instrumentId", "isin", "ISIN"):
        v = ev.get(key)
        if isinstance(v, str) and _looks_like_isin(v):
            return v
    for parent_key in ("details", "action", "instrument"):
        parent = ev.get(parent_key)
        if isinstance(parent, dict):
            for k in ("isin", "ISIN", "instrumentId", "subtitleText", "id", "payload"):
                v = parent.get(k)
                if isinstance(v, str) and _looks_like_isin(v):
                    return v
    return ""


def _amount_value(ev: dict[str, Any]):
    amount = ev.get("amount")
    return amount.get("value") if isinstance(amount, dict) else amount


def parse_timestamp(raw: str) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    # TR sometimes sends "+0000" offsets; fromisoformat handles both in 3.11+.
    try:
        ts = datetime.fromisoformat(s)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def event_id(ev: dict[str, Any]) -> str:
    """TR's own id; a content hash when (rarely) absent."""
    eid = ev.get("id")
    if isinstance(eid, str) and eid:
        return eid
    basis = "|".join(str(ev.get(k, "")) for k in ("eventType", "timestamp", "title", "subtitle"))
    basis += f"|{_amount_value(ev)}"
    return "h:" + hashlib.sha1(basis.encode()).hexdigest()[:32]


def to_decimal(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def transaction_fields(ev: dict[str, Any]) -> dict[str, Any]:
    """All Transaction columns derivable from one raw timeline event."""
    title = (ev.get("title") or "").strip()
    subtitle = (ev.get("subtitle") or "").strip()
    amount = ev.get("amount") if isinstance(ev.get("amount"), dict) else {}
    subtype = (
        ev.get("eventSubType")
        or ev.get("subEventType")
        or (ev.get("details") or {}).get("subType")
        or ""
    )
    return {
        "timestamp": parse_timestamp(ev.get("timestamp") or ev.get("eventTime") or ""),
        "kind": classify(ev),
        "amount": to_decimal(_amount_value(ev)),
        "currency": (amount.get("currency") or "EUR")[:3],
        "title": title[:300],
        "subtitle": subtitle[:300],
        "status": str(ev.get("status") or "")[:40],
        "event_type": str(ev.get("eventType") or "")[:80],
        "event_subtype": str(subtype)[:200],
        "isin": extract_isin(ev),
    }
