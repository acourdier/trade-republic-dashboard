from django.conf import settings
from django.utils import timezone

from broker import tr
from broker.models import SyncRun

NAV = [
    ("overview", "Overview"),
    ("positions", "Positions"),
    ("activity", "Activity"),
    ("income", "Income"),
    ("performance", "Performance"),
    ("settings", "Settings"),
]


def chrome(request):
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else ""
    if current == "position":
        current = "positions"
    last = (SyncRun.objects.filter(status=SyncRun.Status.DONE)
            .exclude(mode=SyncRun.Mode.DOCUMENTS).order_by("-finished_at").first())
    active = SyncRun.objects.filter(status__in=SyncRun.ACTIVE).first()
    age = None
    if last and last.finished_at:
        minutes = (timezone.now() - last.finished_at).total_seconds() / 60
        age = "fresh" if minutes <= 60 else "recent" if minutes <= 24 * 60 else "old"
    return {
        "nav": NAV,
        "nav_current": current,
        "last_sync": last,
        "sync_age": age,
        "active_run": active,
        "has_credentials": tr.load_credentials() is not None,
        "gated": bool(settings.TRDASH_PASSWORD),
    }
