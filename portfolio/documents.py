"""Index the PDFs tr-api downloaded (DOCUMENTS_DIR/manifest.json) into Document rows."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from django.conf import settings

from portfolio.models import Document


def index_documents() -> int:
    root: Path = settings.DOCUMENTS_DIR
    manifest = root / "manifest.json"
    if not manifest.is_file():
        return 0
    try:
        results = json.loads(manifest.read_text(encoding="utf-8")).get("results") or []
    except (json.JSONDecodeError, OSError):
        return 0
    n = 0
    for r in results:
        path = r.get("path")
        if not path or r.get("status") not in ("downloaded", "skipped_existing"):
            continue
        p = Path(path)
        rel = p.relative_to(root) if p.is_absolute() and p.is_relative_to(root) else p
        if not (root / rel).is_file():
            continue
        try:
            d = date.fromisoformat((r.get("event_date") or "")[:10])
        except ValueError:
            d = None
        Document.objects.update_or_create(
            path=str(rel),
            defaults={"title": (r.get("title") or "")[:300], "kind": r.get("kind") or "",
                      "date": d, "isin": r.get("isin") or "",
                      "tr_event_id": r.get("event_id") or ""},
        )
        n += 1
    return n
