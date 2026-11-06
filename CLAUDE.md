# CLAUDE.md

Single-user Trade Republic dashboard. Django 6.1 + SQLite, served by
waitress on 127.0.0.1:8085 with the sync worker in the same process
(`manage.py serve`, which also migrates), run locally with uv or on a server via `compose.yaml`.
Personal use only (BSL-1.1 code inside — see LICENSE).

## Layout

- `broker/` — Trade Republic side.
  - `tr.py`: the **kept, tested** TR code (login, snapshot, timeline). Read its docstring before touching it.
  - `mapping.py`: eventType → `Kind`, pure functions.
  - `sync.py`: one run → DB.
  - `jobs.py`: `SyncRun` queue + worker loop + keepalive.
  - `models.py`: `SyncRun`, `Setting`.
- `portfolio/` — `models.py` (Instrument, Transaction, PortfolioSnapshot, PositionSnapshot, BenchmarkPrice, Document) and `analytics/` (pure functions, cached per sync).
- `ui/` — views, templates, `static/ui/{css,js,fonts,vendor}`. No build step.
- `Dockerfile`, `compose.yaml`, `docker/entrypoint.sh` (installed as `manage`) — server deployment.

## Rules that matter

- **Login is TR's v2 approve-in-app flow.** Poll until `status=CONFIRMED`. `X-TR-App-Version` must be TR's web build (`TR_WEB_APP_VERSION`, currently 2.2645.8); bump it when TR redeploys. No 4-digit codes any more.
- **Both timeline topics are paginated on ONE WebSocket** (`tr.fetch_timeline`). A second WS returns 0 activity items.
- **`compactPortfolioByType` needs `secAccNo`.** Without it TR returns empty positions (the legacy app showed cash only).
- **Withdrawal ≠ card spending.** `net_capital_in = deposits + tax_refunds − withdrawals`; `lifetime_pl = value + card − net_capital_in − income`. See `portfolio/analytics/__init__.py`.
- `Transaction.raw` keeps TR's payload. After changing `mapping.py`, run `manage.py reclassify` (no network).
- Dedupe is by TR event `id` (`tr_id`). A full sync deletes rows TR no longer returns.
- Errors from TR are `broker.tr.TrError` subclasses; `run_sync` never raises, and failures land on the `SyncRun`.
- **Docker is one container, one replica.** All state is in `/data`, and `HOME=/data/home`, so tr-api's `~/.tr-api` profile persists. Chromium (Playwright `--only-shell`) is in the image for the WAF token. The entrypoint `manage` drops root to uid 1000.
- **Off loopback, `TRDASH_PASSWORD` gates every page** (`ui/middleware.py`). The gate exempts only `/login/`, `/healthz` and `/static/`; new routes are gated by default. X-Forwarded-* headers are trusted only with `TRDASH_BEHIND_PROXY=1`.
- Templates autoescape, and chart data goes through `json_script`. The tooltip builds DOM with `textContent`. Keep it that way.
- Design: paper/ink tokens in `app.css` with a selected dark mode, one vermilion accent, no kanji. The chart palette (indigo, ochre, celadon, persimmon, plum) was validated for CVD in that order; don't reorder it. Hero numbers use the sans face; the Mincho serif is for titles only.

## Check before saying done

`uv run pytest && uv run ruff check .`, then restart `uv run python manage.py serve` and look at the pages.
For Docker changes: `docker build -t trdash:test .` and run it against an empty data dir
(the local CLI here lacks buildx, so build with `DOCKER_BUILDKIT=0`).

## Open items

- `timelineDetailV2` enrichment: fill `shares`, `price`, `fees`, `taxes` per trade. `tr.fetch_details` exists but nothing calls it yet.
- The documents download hasn't been run against real TR yet.
