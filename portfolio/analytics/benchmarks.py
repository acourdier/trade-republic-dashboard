"""Benchmark prices (Yahoo daily closes, cached in BenchmarkPrice) and the replay."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from broker.models import Setting
from portfolio.models import BenchmarkPrice

log = logging.getLogger("trdash.benchmarks")

# EUR-listed UCITS ETFs on Euronext Amsterdam — no FX noise.
BENCHMARKS = [
    ("IWDA.AS", "MSCI World"),
    ("VUSA.AS", "S&P 500"),
    ("CNDX.AS", "Nasdaq 100"),
]
REFRESH_AFTER = timedelta(hours=20)


def _fetch_yahoo(symbol: str, start: date, end: date) -> list[tuple[date, float]]:
    p1 = int(datetime.combine(start, time.min, UTC).timestamp())
    p2 = int(datetime.combine(end + timedelta(days=1), time.min, UTC).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={p1}&period2={p2}&interval=1d&events=history")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read())
    result = (payload.get("chart", {}).get("result") or [{}])[0]
    ts = result.get("timestamp") or []
    closes = ((result.get("indicators", {}).get("quote") or [{}])[0].get("close")) or []
    return [(datetime.fromtimestamp(t, UTC).date(), float(c))
            for t, c in zip(ts, closes, strict=False) if c is not None]


def refresh(start: date) -> dict[str, int]:
    """Update stored closes from `start`; at most once per REFRESH_AFTER per symbol."""
    out = {}
    now = datetime.now(UTC)
    for symbol, _ in BENCHMARKS:
        key = f"benchmark_fetched:{symbol}"
        last = Setting.get(key)
        if last and now - datetime.fromisoformat(last) < REFRESH_AFTER:
            continue
        try:
            rows = _fetch_yahoo(symbol, start, now.date())
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            log.info("benchmark %s fetch failed: %s", symbol, e)
            continue
        BenchmarkPrice.objects.bulk_create(
            [BenchmarkPrice(symbol=symbol, date=d, close=Decimal(f"{c:.4f}")) for d, c in rows],
            update_conflicts=True, unique_fields=["symbol", "date"], update_fields=["close"],
        )
        Setting.put(key, now.isoformat())
        out[symbol] = len(rows)
    return out


def closes(symbol: str) -> dict[date, float]:
    return {d: float(c) for d, c in
            BenchmarkPrice.objects.filter(symbol=symbol).values_list("date", "close")}


def replay(cost_basis: list[tuple[date, float]], bench: dict[date, float],
           today: date) -> list[tuple[date, float]]:
    """Invest each change of the user's cost basis into the benchmark that day.

    On every day the cost basis moves, buy/sell the delta worth of benchmark
    units at the last known close; emit units × close every calendar day,
    carrying the last close over weekends and holidays.
    """
    if not cost_basis or not bench:
        return []
    cb = dict(cost_basis)
    cur, end = min(cb), max(max(bench), today)
    units, prev, last_close, out = 0.0, None, None, []
    while cur <= end:
        if cur in bench:
            last_close = bench[cur]
        if cur in cb:
            delta = cb[cur] if prev is None else cb[cur] - prev
            if delta and last_close:
                units += delta / last_close
            prev = cb[cur]
        if last_close and units:
            out.append((cur, round(units * last_close, 2)))
        cur += timedelta(days=1)
    return out
