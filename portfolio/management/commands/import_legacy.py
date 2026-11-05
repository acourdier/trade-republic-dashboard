"""TEMPORARY bridge: load legacy/DATA into the DB so the app has data before the
first real sync. The first full sync replaces these rows (their tr_id is
"legacy:…", which TR never returns). Delete with legacy/ at cleanup."""

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from broker.mapping import parse_timestamp, to_decimal
from portfolio.models import Instrument, Kind, PortfolioSnapshot, Transaction

TYPE_TO_KIND = {
    "Deposit": Kind.DEPOSIT, "Removal": Kind.CARD, "Withdrawal": Kind.WITHDRAWAL,
    "Tax Refund": Kind.TAX_REFUND, "Buy": Kind.BUY, "Sell": Kind.SELL,
    "Dividend": Kind.DIVIDEND, "Interest": Kind.INTEREST,
    "Bond redemption": Kind.BOND_REDEMPTION, "Cancelled": Kind.CANCELLED,
    "Expired": Kind.EXPIRED, "Rejected": Kind.REJECTED, "Pending": Kind.PENDING,
}


class Command(BaseCommand):
    help = "Import legacy/DATA (account_transactions.csv + cash) — temporary."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=str(settings.BASE_DIR / "legacy" / "DATA"))

    @transaction.atomic
    def handle(self, *args, dir, **opts):
        base = Path(dir)
        Transaction.objects.filter(tr_id__startswith="legacy:").delete()
        rows, seen = [], set()
        with (base / "account_transactions.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter=";"):
                ts = parse_timestamp(r["Date"])
                if ts is None:
                    continue
                key = f"{r['Date']}|{r['Type']}|{r['Value']}|{r['Note']}"
                if key in seen:
                    continue
                seen.add(key)
                isin = r.get("ISIN") or ""
                if isin:
                    Instrument.objects.get_or_create(isin=isin, defaults={"name": r["Note"]})
                rows.append(Transaction(
                    tr_id="legacy:" + hashlib.sha1(key.encode()).hexdigest()[:32],
                    source="legacy", timestamp=ts,
                    kind=TYPE_TO_KIND.get(r["Type"], Kind.UNKNOWN),
                    amount=to_decimal(r["Value"]), title=r["Note"][:300],
                    event_type=r.get("EventType", "")[:80],
                    event_subtype=r.get("EventSubType", "")[:200],
                    instrument_id=isin or None, raw={"legacy_csv": r},
                ))
        Transaction.objects.bulk_create(rows, batch_size=500)
        pj = base / "portfolio.json"
        if pj.is_file() and not PortfolioSnapshot.objects.exists():
            s = json.loads(pj.read_text())["summary"]
            PortfolioSnapshot.objects.create(
                taken_at=timezone.now(), cash=Decimal(str(s["cash_eur"])),
                depot_value=Decimal(str(s["depot_netvalue"])),
                depot_cost=Decimal(str(s["depot_buycost"])), raw={"legacy": True},
            )
        self.stdout.write(f"imported {len(rows)} legacy transactions")
