from django.core.management.base import BaseCommand

from broker.sync import reclassify
from portfolio.analytics import history


class Command(BaseCommand):
    help = "Re-run the event mapping over stored raw TR payloads (no network)."

    def handle(self, *args, **opts):
        self.stdout.write(f"{reclassify()} transaction(s) reclassified")
        self.stdout.write(f"value history rebuilt: {history.rebuild()['days']} day(s)")
