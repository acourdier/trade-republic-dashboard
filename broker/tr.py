"""Trade Republic integration — the code kept from the legacy tr_fetch.py.

Tested against real TR on 2026-11-03. What must not change without
re-testing against a real account:

* Login is TR's v2 **approve-in-app** flow. There is no 4-digit code any
  more; TR pushes an approve/deny prompt and we poll the login process
  ourselves until status=CONFIRMED (tr-api's poll_login_approval doesn't
  accept CONFIRMED, which is what TR's web frontend treats as approved).
* X-TR-App-Version must be TR's *web frontend build* (settings.
  TR_WEB_APP_VERSION). tr-api's default "15.7.0" gets a 200 + processId,
  but no prompt ever reaches the phone.
* After approval, GET the session endpoint so TR sets/rotates the session
  cookies before we save them.
* timelineTransactions and timelineActivityLog MUST be paginated on the SAME
  WebSocket: a second, separate WS returns 0 activity items.

Errors are raised as TrError subclasses; the worker turns them into a failed
SyncRun with a readable message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from tr_api import Profile, TrClient, account, auth, profiles
from tr_api import accounts as tr_accounts
from tr_api import activity_log as tr_activity_log
from tr_api import portfolio as tr_portfolio
from tr_api import transactions as tr_transactions
from tr_api.auth import InvalidCredentials, LoginError, RateLimited
from tr_api.exceptions import MissingSessionCookies, ProfileNotFound, SessionExpired, TrApiError

log = logging.getLogger("trdash.broker")

auth.TR_WEB_APP_VERSION = settings.TR_WEB_APP_VERSION

V2_APPROVAL_TIMEOUT_SEC = 120.0  # TR's own window
V2_POLL_INTERVAL_SEC = 2.0
V2_DONE_STATUSES = ("CONFIRMED", "COMPLETED", "APPROVED")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class TrError(Exception):
    code = "tr_error"


class NotConfigured(TrError):
    code = "not_configured"


class BadCredentials(TrError):
    code = "bad_credentials"


class LoginRequired(TrError):
    """Approval timed out / was rejected, or the session died mid-fetch."""

    code = "login_required"


class RateLimitedError(TrError):
    code = "rate_limited"


class TrUnavailable(TrError):
    """Network or TR API error — usually transient."""

    code = "tr_unavailable"


# ---------------------------------------------------------------------------
# Credentials: data/secrets/credentials (line 1 phone E.164, line 2 PIN), 0600
# ---------------------------------------------------------------------------
CREDENTIALS_FILE: Path = settings.SECRETS_DIR / "credentials"
LEGACY_CREDENTIALS_FILE = Path.home() / ".pytr" / "credentials"


@dataclass(frozen=True)
class Credentials:
    phone: str
    pin: str


def load_credentials() -> Credentials | None:
    for path in (CREDENTIALS_FILE, LEGACY_CREDENTIALS_FILE):
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) >= 2 and lines[0].startswith("+") and lines[1].strip():
            creds = Credentials(lines[0].strip(), lines[1].strip())
            if path != CREDENTIALS_FILE:  # one-time import from the legacy location
                save_credentials(creds)
            return creds
    return None


def save_credentials(creds: Credentials) -> None:
    tmp = CREDENTIALS_FILE.with_suffix(".tmp")
    tmp.write_text(f"{creds.phone}\n{creds.pin}\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(CREDENTIALS_FILE)


def delete_credentials() -> None:
    CREDENTIALS_FILE.unlink(missing_ok=True)


def require_credentials() -> Credentials:
    creds = load_credentials()
    if creds is None:
        raise NotConfigured("No Trade Republic credentials yet — add them in Settings.")
    return creds


def ensure_profile(phone: str) -> Profile:
    try:
        return profiles.load(phone)
    except ProfileNotFound:
        return profiles.create(phone, jurisdiction="DE", name="Dashboard")


def forget_session(phone: str) -> None:
    """Drop TR session cookies so the next sync does a fresh login."""
    with contextlib.suppress(OSError, TrApiError):
        ensure_profile(phone).cookies_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Login (v2 approve-in-app)
# ---------------------------------------------------------------------------
def _v2_process_summary(j: dict[str, Any]) -> str:
    keep = {k: v for k, v in j.items() if not any(t in k.lower() for t in ("id", "token"))}
    return json.dumps(keep, separators=(",", ":"), default=str)[:120]


def _v2_error_code(r) -> str:
    try:
        return str(r.json()["errors"][0]["errorCode"])
    except (ValueError, KeyError, IndexError, TypeError):
        return f"HTTP {r.status_code}"


def _poll_login_v2(session, process_id: str, headers: dict[str, str]) -> None:
    url = f"{auth.API_BASE}{auth.LOGIN_ENDPOINT_V2}/processes/{process_id}"
    deadline = time.time() + V2_APPROVAL_TIMEOUT_SEC
    last = "no response"
    while time.time() < deadline:
        try:
            r = session.get(url, headers=headers, timeout=15)
        except Exception as e:  # ConnectionError / Timeout — retry until deadline
            last = f"network error ({type(e).__name__})"
            time.sleep(V2_POLL_INTERVAL_SEC)
            continue
        if r.status_code >= 400:
            code = _v2_error_code(r)
            if code == "TOO_MANY_REQUESTS":
                raise RateLimitedError("Rate-limited by Trade Republic. Wait 15–30 min.")
            if code in ("PROCESS_GONE", "ALREADY_PROCESSED", "NOT_FOUND"):
                raise LoginRequired(
                    f"The login request expired or was rejected in the app ({code}). "
                    "Sync again and approve the prompt."
                )
            raise TrUnavailable(f"Login status check failed: {code}")
        try:
            j = r.json()
        except ValueError:
            j = {}
        status = str(j.get("status") or j.get("state") or "").upper()
        last = _v2_process_summary(j) if j else f"HTTP {r.status_code}, empty body"
        if status in V2_DONE_STATUSES:
            return
        if j.get("requiredAction") == "AUTHENTICATOR_VERIFICATION":
            raise TrUnavailable(
                "This account requires an authenticator-app code at login, "
                "which isn't supported yet."
            )
        if status and status != "PENDING":
            raise TrUnavailable(f"Unexpected login status from Trade Republic: {status}")
        time.sleep(V2_POLL_INTERVAL_SEC)
    raise LoginRequired(f"Not approved in the Trade Republic app in time. TR said: {last}")


def _approve_login_v2(prof: Profile, pin: str) -> TrClient:
    import requests
    from tr_api import cookies as _c
    from tr_api import waf

    device_id = auth.device_id_for(prof)
    session = requests.Session()
    try:
        init = auth.initiate_login_v2(prof.phone, pin, device_id, session=session)
    except RateLimited as e:
        raise RateLimitedError(
            f"Rate-limited by Trade Republic. Retry after {e.next_attempt_at} "
            f"(≈ {(e.wait_seconds or 0) // 60} min)."
        ) from e
    except InvalidCredentials as e:
        raise BadCredentials(f"Phone or PIN rejected: {e}") from e
    except LoginError as e:
        raise TrUnavailable(f"Login failed: {e}") from e

    headers = auth._login_headers_v2(waf.get_waf_token().value, device_id)
    _poll_login_v2(session, init.process_id, headers)

    # Like pytr: hit the session endpoint so TR sets/rotates session cookies.
    # (Failures are fine here: TrClient validation below reports missing cookies.)
    with contextlib.suppress(Exception):
        session.get(f"{auth.API_BASE}{auth.SESSION_ENDPOINT}", headers=headers, timeout=15)
    jar = {c.name: c.value for c in session.cookies if c.domain.endswith("traderepublic.com")}
    _c.save_to_file(jar, prof.cookies_file)
    try:
        return TrClient(prof)
    except MissingSessionCookies as e:
        raise TrUnavailable(
            "Login approved but TR's session cookies were incomplete "
            f"(got: {', '.join(sorted(jar)) or 'none'})."
        ) from e


def authenticated_client(
    creds: Credentials, on_awaiting_approval: Callable[[], None] | None = None
) -> TrClient:
    """Reuse the saved session if TR still accepts it, else run the v2 login."""
    prof = ensure_profile(creds.phone)
    profiles.set_active(creds.phone)
    try:
        client = TrClient(prof)
        try:
            if account.ping(client):
                return client
        except TrApiError as e:
            raise TrUnavailable(f"Network/API error during session check: {e}") from e
    except MissingSessionCookies:
        pass
    if on_awaiting_approval:
        on_awaiting_approval()
    return _approve_login_v2(prof, creds.pin)


def refresh_session(phone: str) -> bool:
    """pytr-style keepalive: rotate cookies before TR's ~5 min idle timeout."""
    try:
        prof = profiles.load(phone)
    except ProfileNotFound:
        return False
    if not prof.cookies_file.is_file():
        return False
    result = auth.refresh_session(prof)
    if not result.ok:
        log.info("session refresh failed: %s", result.error)
    return result.ok


def _wrap_session_errors(what: str):
    """Decorator: translate tr-api errors raised during a fetch."""

    def deco(fn):
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except SessionExpired as e:
                raise LoginRequired(f"Session expired during {what}. Sync again.") from e
            except RateLimited as e:
                raise RateLimitedError(f"Rate-limited during {what}: {e}") from e
            except TrApiError as e:
                raise TrUnavailable(f"{what.capitalize()} failed: {e}") from e

        return inner

    return deco


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
def _ticker_price(tick: Any) -> Any:
    if not isinstance(tick, dict):
        return None
    for side in ("last", "bid", "ask"):
        q = tick.get(side)
        if isinstance(q, dict) and q.get("price") is not None:
            return q["price"]
    return None


async def _snapshot_async(client: TrClient, sec_acc_no: str | None) -> dict[str, Any]:
    """Named + priced positions, cash and TR categories on ONE WebSocket.

    Based on tr-api's snapshot_full, with one fix: compactPortfolioByType is
    requested WITH secAccNo. TR (v3x) returns an empty flat compactPortfolio
    and, without secAccNo, an empty by-type view too — the legacy app showed
    0 positions / cash only because of this.
    """
    from tr_api.portfolio import _is_bond_name
    from tr_api.protocol import TrWebSocket

    by_type_payload: dict[str, Any] = {"type": tr_portfolio.TOPIC_COMPACT_PORTFOLIO_BY_TYPE}
    if sec_acc_no:
        by_type_payload["secAccNo"] = sec_acc_no
    async with TrWebSocket(client.session.cookies) as ws:
        compact, by_type, cash = await ws.batch_fetch(
            [{"type": tr_portfolio.TOPIC_COMPACT_PORTFOLIO}, by_type_payload,
             {"type": tr_portfolio.TOPIC_CASH}],
            timeout=20,
        )
        compact, by_type = compact or {}, by_type or {}

        categories: dict[str, str] = {}
        by_type_positions: list[dict[str, Any]] = []
        for cat in by_type.get("categories") or []:
            cat_type = str(cat.get("categoryType") or "others")
            for pos in cat.get("positions") or []:
                isin = str(pos.get("isin") or pos.get("instrumentId") or "")
                if isin:
                    categories[isin] = cat_type
                    by_type_positions.append({**pos, "instrumentId": isin})

        flat = list(compact.get("positions") or [])
        positions = flat or by_type_positions
        diag = {"flat": len(flat), "by_type": len(by_type_positions),
                "sec_acc_no": bool(sec_acc_no)}
        isins = [str(p.get("instrumentId") or "") for p in positions]
        isins = [i for i in isins if i]

        instruments = await ws.batch_fetch(
            [{"type": "instrument", "id": i} for i in isins], timeout=90
        ) if isins else []
        inst_by_isin = {i: d for i, d in zip(isins, instruments, strict=True)
                        if isinstance(d, dict)}

        tick_keys, tick_payloads = [], []
        for i in isins:
            ex = (inst_by_isin.get(i) or {}).get("exchangeIds") or []
            first = ex[0] if ex else None
            ex_id = first if isinstance(first, str) else (first or {}).get("slug") \
                if isinstance(first, dict) else None
            if ex_id:
                tick_keys.append(i)
                tick_payloads.append({"type": "ticker", "id": f"{i}.{ex_id}"})
        ticks = await ws.batch_fetch(tick_payloads, timeout=90) if tick_payloads else []
        tick_by_isin = dict(zip(tick_keys, ticks, strict=True))

    merged = []
    for raw in positions:
        isin = str(raw.get("instrumentId") or "")
        inst = inst_by_isin.get(isin) or {}
        name = (inst.get("shortName") or inst.get("name")
                or ((inst.get("derivativeInfo") or {}).get("underlying") or {}).get("shortName")
                or raw.get("name") or "")
        price = _ticker_price(tick_by_isin.get(isin))
        # Bonds are quoted in % of face value.
        if price is not None and _is_bond_name(name):
            with contextlib.suppress(TypeError, ValueError):
                price = str(float(price) / 100.0)
        merged.append({**raw, "isin": isin, "name": name,
                       "exchangeIds": inst.get("exchangeIds") or [],
                       "instrumentType": inst.get("typeId") or "",
                       "currentPrice": price if price is not None else raw.get("currentPrice")})
    return {"portfolio": {"positions": merged}, "cash": cash, "categories": categories,
            "diagnostics": diag, "by_type_raw": by_type}


@_wrap_session_errors("portfolio fetch")
def fetch_portfolio(client: TrClient) -> dict[str, Any]:
    pair = tr_accounts.account_pairs(client).default_pair()
    sec_acc_no = pair.securities_account_number if pair is not None else None
    snap = asyncio.run(_snapshot_async(client, sec_acc_no))
    log.info("portfolio snapshot: %s", snap["diagnostics"])
    return snap


# ---------------------------------------------------------------------------
# Timeline (both topics, one WebSocket)
# ---------------------------------------------------------------------------
async def _paginate_topic_on_ws(ws, topic: str, *, cutoff=None, max_pages: int = 2000):
    """Paginate one timeline topic on an EXISTING WS until cursor is None.

    max_pages is a safety cap (~30 items/page → ~60k events). With `cutoff`,
    stops at the first item older than it.
    """
    from broker.mapping import parse_timestamp

    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(max_pages):
        payload = {"type": topic}
        if cursor is not None:
            payload["after"] = cursor
        page = await ws.fetch_one(payload)
        page_items = page.get("items") or []
        if cutoff is not None:
            for it in page_items:
                ts = parse_timestamp(it.get("timestamp") or it.get("eventTime") or "")
                if ts is not None and ts < cutoff:
                    return items
                items.append(it)
        else:
            items.extend(page_items)
        cursor = (page.get("cursors") or {}).get("after")
        if cursor is None:
            break
    return items


@_wrap_session_errors("transactions fetch")
def fetch_timeline(
    client: TrClient, cutoff: datetime | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(timelineTransactions items, timelineActivityLog items) on ONE WebSocket."""
    from tr_api.protocol import TrWebSocket

    async def _go():
        async with TrWebSocket(client.session.cookies) as ws:
            tx = await _paginate_topic_on_ws(ws, tr_transactions.TOPIC, cutoff=cutoff)
            act = await _paginate_topic_on_ws(ws, tr_activity_log.TOPIC, cutoff=cutoff)
            return tx, act

    return asyncio.run(_go())


@_wrap_session_errors("event detail fetch")
def fetch_details(client: TrClient, event_ids: list[str]) -> dict[str, dict[str, Any]]:
    from tr_api import timeline_detail

    return timeline_detail.fetch_many(client, event_ids)


@_wrap_session_errors("documents download")
def download_documents(client: TrClient, out_dir: Path, phone: str):
    from tr_api import documents

    return documents.download_all(
        client, out_dir, keepalive=lambda: refresh_session(phone), write_manifest=True
    )
