"""ECB series cached in PricePoint: deposit facility rate and euro-area HICP.

Public data, fetched at most once per REFRESH_AFTER from the ECB Data Portal.
The deposit rate is what Trade Republic pays on cash; HICP turns nominal euros
into today's euros on the projection page.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from broker.models import Setting
from portfolio.models import PricePoint

log = logging.getLogger("trdash.ecb")

DFR = "ECB:DFR"  # deposit facility rate, % a year
HICP = "ECB:HICP"  # euro-area HICP, all items, index (2015 = 100)
SERIES = {
    DFR: "FM/D.U2.EUR.4F.KR.DFR.LEV",
    HICP: "ICP/M.U2.N.000000.4.INX",
}
URL = ("https://data-api.ecb.europa.eu/service/data/{key}"
       "?format=csvdata&detail=dataonly&startPeriod={start}")
REFRESH_AFTER = timedelta(hours=20)
LOOKBACK = timedelta(days=400)  # so the rate/index in force at `start` is included


def parse_csv(text: str) -> list[tuple[date, float]]:
    """ECB csvdata → [(date, value)]. Monthly periods ("2025-06") map to the 1st."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        period, value = row.get("TIME_PERIOD") or "", row.get("OBS_VALUE") or ""
        try:
            day = (date.fromisoformat(period) if len(period) == 10
                   else date.fromisoformat(f"{period}-01"))
            out.append((day, float(value)))
        except ValueError:
            continue
    return sorted(out)


def changes_only(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Daily rate series → the days it changed (plus the first)."""
    out: list[tuple[date, float]] = []
    for d, v in points:
        if not out or out[-1][1] != v:
            out.append((d, v))
    return out


def _fetch(key: str, start: date) -> list[tuple[date, float]]:
    req = urllib.request.Request(URL.format(key=key, start=start.isoformat()),
                                 headers={"Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return parse_csv(r.read().decode("utf-8"))


def refresh(start: date) -> dict[str, int]:
    out = {}
    now = datetime.now(UTC)
    for series, key in SERIES.items():
        stamp = f"ecb_fetched:{series}"
        last = Setting.get(stamp)
        if last and now - datetime.fromisoformat(last) < REFRESH_AFTER:
            continue
        try:
            points = _fetch(key, start - LOOKBACK)
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeDecodeError) as e:
            log.info("ECB %s fetch failed: %s", series, e)
            continue
        if series == DFR:
            points = changes_only(points)
        PricePoint.objects.bulk_create(
            [PricePoint(series=series, date=d, value=Decimal(f"{v:.6f}")) for d, v in points],
            update_conflicts=True, unique_fields=["series", "date"], update_fields=["value"],
        )
        Setting.put(stamp, now.isoformat())
        out[series] = len(points)
    return out


def series(name: str) -> list[tuple[date, float]]:
    return [(d, float(v)) for d, v in
            PricePoint.objects.filter(series=name).order_by("date").values_list("date", "value")]
