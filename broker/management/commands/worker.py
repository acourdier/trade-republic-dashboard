from django.core.management.base import BaseCommand

from broker.jobs import work_forever


class Command(BaseCommand):
    help = "Run queued syncs and keep the Trade Republic session alive (standalone worker)."

    def handle(self, *args, **opts):
        work_forever()
