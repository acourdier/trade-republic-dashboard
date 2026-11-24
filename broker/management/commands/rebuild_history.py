from django.core.management.base import BaseCommand

from portfolio.analytics import history


class Command(BaseCommand):
    help = "Recompute the daily value history from stored trades and prices (no network)."

    def handle(self, *args, **opts):
        rep = history.rebuild()
        self.stdout.write(
            f"{rep['days']} day(s) rebuilt · {rep['instruments']} instrument(s), "
            f"{rep['priced']} with prices · trades: {rep['exact']} exact, "
            f"{rep['estimated']} estimated · cash gap {rep['cash_gap']}, "
            f"depot gap {rep['depot_gap']} (vs snapshot {rep['checked_on']})"
        )
