from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Serve the dashboard on 127.0.0.1 (waitress) with the sync worker in-process."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=settings.PORT)
        parser.add_argument("--no-worker", action="store_true")

    def handle(self, *args, port, no_worker, **opts):
        from waitress import serve

        from broker.jobs import start_worker_thread
        from trdash.wsgi import application

        if not no_worker:
            start_worker_thread()
        self.stdout.write(f"Trade Republic dashboard → http://localhost:{port}/")
        serve(application, host="127.0.0.1", port=port, threads=8, ident="trdash")
