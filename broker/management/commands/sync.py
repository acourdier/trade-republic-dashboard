from django.core.management.base import BaseCommand

from broker.models import SyncRun
from broker.sync import run_sync


class Command(BaseCommand):
    help = "Run one sync now, in the foreground (approve the login in the TR app if asked)."

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group()
        g.add_argument("--full", action="store_true", help="Re-fetch the whole history.")
        g.add_argument("--documents", action="store_true", help="Download all PDFs.")

    def handle(self, *args, full, documents, **opts):
        mode = (SyncRun.Mode.DOCUMENTS if documents
                else SyncRun.Mode.FULL if full else SyncRun.Mode.INCREMENTAL)
        run = SyncRun.objects.create(mode=mode, status=SyncRun.Status.RUNNING)
        self.stdout.write(f"Sync #{run.pk} ({mode}) — if asked, approve in the TR app…")
        run_sync(run)
        run.refresh_from_db()
        self.stdout.write(f"{run.status}: {run.message or run.counts}")
        if run.status != SyncRun.Status.DONE:
            raise SystemExit(1)
