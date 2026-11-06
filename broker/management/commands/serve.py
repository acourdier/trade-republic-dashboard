from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Migrate, then serve the dashboard (waitress) with the sync worker in-process."

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.HOST)
        parser.add_argument("--port", type=int, default=settings.PORT)

    def handle(self, *args, host, port, **opts):
        from waitress import serve

        from broker.jobs import start_worker_thread
        from trdash.wsgi import application

        call_command("migrate", interactive=False, verbosity=0)
        start_worker_thread()
        if host not in ("127.0.0.1", "localhost", "::1") and not settings.TRDASH_PASSWORD:
            self.stderr.write("Warning: listening beyond loopback without TRDASH_PASSWORD set.")
        self.stdout.write(f"Trade Republic dashboard → http://{host}:{port}/")
        serve(
            application,
            host=host,
            port=port,
            threads=8,
            ident="trdash",
            **(
                # X-Forwarded-* are honoured only when TRDASH_BEHIND_PROXY=1, and
                # then the proxy must be the only thing that can reach this port.
                {
                    "trusted_proxy": "*",
                    "trusted_proxy_headers": {
                        "x-forwarded-proto",
                        "x-forwarded-host",
                        "x-forwarded-for",
                    },
                }
                if settings.BEHIND_PROXY
                else {}
            ),
        )
