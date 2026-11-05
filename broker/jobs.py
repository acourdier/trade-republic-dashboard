"""SyncRun queue + the worker loop (sync jobs and the session keepalive)."""

from __future__ import annotations

import logging
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

from broker import tr
from broker.models import SyncRun
from broker.sync import run_sync

log = logging.getLogger("trdash.worker")

KEEPALIVE_INTERVAL_SEC = 290  # TR's server-side session idles out after ~5 min
POLL_INTERVAL_SEC = 1.0


def enqueue(mode: str = SyncRun.Mode.INCREMENTAL) -> SyncRun:
    """Queue a sync unless one is already in flight (then return that one)."""
    active = SyncRun.objects.filter(status__in=SyncRun.ACTIVE).first()
    return active or SyncRun.objects.create(mode=mode)


def _recover_stale_runs() -> None:
    n = SyncRun.objects.filter(
        status__in=[SyncRun.Status.RUNNING, SyncRun.Status.AWAITING_APPROVAL]
    ).update(status=SyncRun.Status.FAILED, error_code="interrupted",
             message="The app stopped while this sync was running.",
             finished_at=timezone.now())
    if n:
        log.info("marked %d interrupted sync run(s) as failed", n)


def _keepalive() -> None:
    creds = tr.load_credentials()
    if creds is None:
        return
    try:
        ok = tr.refresh_session(creds.phone)
        log.debug("keepalive ok=%s", ok)
    except Exception as e:  # never let the keepalive kill the worker
        log.info("keepalive failed: %s", e)


def work_forever(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    _recover_stale_runs()
    next_keepalive = time.monotonic() + 60
    log.info("worker started")
    while not stop.is_set():
        close_old_connections()
        run = SyncRun.objects.filter(status=SyncRun.Status.QUEUED).order_by("created_at").first()
        if run is not None:
            run_sync(run)
            next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL_SEC
            continue
        if time.monotonic() >= next_keepalive:
            _keepalive()
            next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL_SEC
        stop.wait(POLL_INTERVAL_SEC)


def start_worker_thread() -> threading.Thread:
    t = threading.Thread(target=work_forever, name="trdash-worker", daemon=True)
    t.start()
    return t
