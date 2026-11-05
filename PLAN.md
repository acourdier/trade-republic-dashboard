# Rework plan — Trade Republic dashboard (Django + SQLite)

Status: approved 2026-11-05 — fresh git repo, personal use only, Japanese feel through style only (no kanji)

## Progress (2026-11-05)

- **Done:** phases 0–5, and most of 6.
  - Skeleton, broker port, worker and sync dialog, analytics, design system and all six pages.
  - CSV export, documents download and indexing, `reclassify`.
- **Verified offline:**
  - 22 tests pass.
  - Analytics on the imported legacy CSV reproduce the legacy `analytics.json` exactly (deposits, buys, sells, net capital, lifetime P&L, income, forward dividends).
  - Every page renders in light, dark and at phone width.
- **Not yet verified against real TR:**
  - The login and sync (needs an approval on the phone).
  - The `secAccNo` fix for the empty-positions bug.
  - The documents download.
- **Still to do:**
  - `timelineDetailV2` enrichment (shares, fees, taxes per trade).
  - Phase 7: after a successful real full sync, delete `legacy/` and `portfolio/management/commands/import_legacy.py`.
- **Changes from the original plan:**
  - No htmx: plain JS polling was enough.
  - Headline figures use Inter; the Mincho serif is for titles only.

## Goal

Keep what the app is for (monitoring one Trade Republic account: positions,
performance, income, history) and rebuild everything else. The only code
carried over is the Trade Republic integration from `app/tr_fetch.py`: the
v2 approve-in-app login, the portfolio snapshot, the single-WebSocket timeline
pagination, and the event-type mapping. All of it was tested against real TR
on 2026-11-03.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Framework | **Django 6.1.x** (pinned `Django>=6.1,<6.3`) | No 6.x LTS exists yet. 5.2 is the current LTS; **6.2 LTS is due ~April 2027**. Starting on 6.1 means the move to 6.2 LTS is a version bump. |
| Python | 3.13 (already in `.venv`) | Django 6 needs ≥ 3.12. |
| Database | **SQLite**, WAL mode, one file at `data/trdash.sqlite3` | Portable: copy one file to back up or move. |
| Secrets | PIN in a `0600` file `data/secrets/credentials`; TR session cookies stay in `~/.tr-api/profiles/<phone>/` | The DB holds no secrets, so it can be copied or backed up freely. |
| Background work | A `manage.py worker` command that (a) runs queued sync jobs from a `SyncRun` table and (b) refreshes the TR session every ~5 min | The login blocks up to 120 s and Playwright doesn't belong in a request thread. Uses the DB as the queue, so no Redis or Celery. |
| Web server | `waitress` on `127.0.0.1:8085` | Pure Python, simple, bound to loopback only. |
| Frontend | Django templates + **htmx** + vanilla JS + **Chart.js** (vendored), self-hosted fonts | No Node build step. Template autoescaping replaces the hand-rolled `innerHTML` escaping (commit 7307697). |
| Packaging | `pyproject.toml` + **uv**; `tr-api` pinned to git commit `4441a8b` | Reproducible. Keeps the existing rule never to `pip install tr-api` from PyPI (the name is unregistered there). |
| Auth | None (single user, loopback only); Django CSRF on all POSTs | Replaces the custom Origin/Host checks in `server.py`. |
| Tests | `pytest-django` + `ruff` | The current repo has none. |

## Architecture

```
trdash/                     Django project: settings, urls, waitress entry
broker/                     Trade Republic adapter (the kept code)
  tr.py                     login v2, client, snapshot, timeline WS pagination
  mapping.py                EVENT_TYPE_MAP, trade/corporate-action classifiers, ISIN extraction
  sync.py                   orchestrates a sync and writes models
  management/commands/      sync [--full], worker, reclassify
portfolio/                  domain models + analytics
  models.py
  analytics/                pure functions: cashflow, xirr, income, benchmark, allocation
  views.py, urls.py
ui/                         base templates, design tokens, components, static
data/                       gitignored: sqlite db, secrets, documents/, logs
```

How the TR code gets ported: copy its logic as-is, but **replace `sys.exit(N)`
with typed exceptions** (`LoginRequired`, `LoginTimeout`, `BadCredentials`,
`RateLimited`, `TrUnavailable`). The worker turns these into `SyncRun.status`
and a readable message. The behaviours we fixed stay exactly as they are:
polling until `CONFIRMED`, the `X-TR-App-Version=2.2645.8` override (via the
`TR_WEB_APP_VERSION` setting), the session-endpoint hit after approval, and
the single WebSocket for both timeline topics. The v1 4-digit MFA path is
dropped.

## Data model (SQLite)

- **Account**: phone, display name, jurisdiction. A single row.
- **Instrument**: ISIN (PK), name, TR category, asset type, currency, domicile (from the ISIN prefix), logo URL.
- **Transaction**: one row per TR timeline event.
  - `tr_id` UNIQUE (TR's event id, which gives real dedupe instead of today's `Date|Type|Value|Note` key)
  - `timestamp`, `kind` (deposit, withdrawal, card, tax_refund, buy, sell, dividend, interest, bond_redemption, cancelled, …)
  - `amount`, `currency`, `instrument` FK
  - `title`, `subtitle`, `event_type`, `event_subtype`
  - `shares`, `price`, `fees`, `taxes` (nullable, filled by the detail enrichment)
  - **`raw` JSON**
- **PortfolioSnapshot**: one per sync. `taken_at`, cash, depot value, depot cost, total. This builds a *real* net-worth history from now on, alongside the reconstructed cost-basis line.
- **PositionSnapshot**: snapshot FK, instrument FK, qty, avg cost, price, value. Gives per-position history over time.
- **BenchmarkPrice**: symbol, date, close. Replaces `DATA/benchmark_cache/*.json`.
- **SyncRun**:
  - `created`, `started`, `finished`, `mode` (incremental/full)
  - `status` (queued, awaiting_approval, fetching, done, failed), `stage`, `message`, `counts`
  - Replaces exit codes, `last_update.date` and the log grepping.
- **Document**: downloaded PDF path, kind, year, transaction FK.
- **Setting**: key/value (documents path, base currency, benchmarks…).

Storing `raw` means `manage.py reclassify` can re-run the mapping after TR
renames event types again, with no re-fetch.

## Analytics

Port `analyze_analytics.py` into pure functions over querysets. At ~6k rows,
computing on request is cheap; results are cached per `SyncRun`. Formulas stay
the same: `net_capital_in`, `lifetime_pl`, XIRR, forward dividends,
yield-on-cost, top/bottom contributors, benchmark replay (Yahoo `IWDA.AS`,
`VUSA.AS`, `CNDX.AS`), and allocation by category and by domicile.

New, made possible by the DB:
- per-position page: trades, income, cost basis, value over snapshots
- realized vs unrealized P&L per ISIN, once `shares`/`price` are filled from `timelineDetailV2`, which tr-api already exposes

**Parity gate:** before the old code is deleted, the new analytics must
reproduce the current `DATA/analytics.json` KPIs on the same data, within
rounding. This becomes a test with a fixture.

## Frontend: "less is more", Japanese touch

Principles: 間 *ma* (generous whitespace), hairlines instead of boxes and
shadows, one accent colour used rarely, numbers as the main visual element.
No emoji.

- **Palette**: washi paper `#F6F4EF` background, sumi ink `#1C1C1A` text, stone grey `#8A867E` secondary, a single shu/vermilion accent `#C53D2B` (logo seal, active nav, focus). Gains in matsuba green `#3F6B4E`, losses in a muted beni red `#A8443A`. Dark mode is the inverse: sumi `#141412`, paper text, same accent.
- **Type**: Shippori Mincho for page titles and headline figures; Inter (tabular numerals) for UI and tables. Self-hosted, no third-party font calls.
- **Motifs**: a small vermilion *hanko*-style square as the logo. No kanji: the Japanese feel comes only from palette, type and spacing. Charts in thin ink lines, muted fills, no gridline clutter.
- **Layout**: top nav, content at max ~1100px, large KPI figures, tables with row hairlines only. Works at phone width.

### Pages (8 today → 6)

| Page | Contents | Replaces |
|---|---|---|
| Overview | Net worth, total P&L, XIRR, cash, allocation bar, net-worth chart (real snapshots + cost basis), biggest movers | `index.html` cockpit |
| Positions | Sortable table grouped by category; **detail page per ISIN** | `index.html` tables + modal |
| Activity | Unified ledger + orders, filters (kind, month, search), CSV export | `ledger.html`, `orders.html` |
| Income | Dividends & interest by month and issuer, forward 12-month estimate, yield-on-cost | `dividends.html` |
| Performance | Cash flow in/out, lifetime P&L, benchmarks, allocation by domicile, methodology notes | `analytics.html`, `glossary.html` |
| Settings | Account & PIN, sync history, documents download, reset | `settings.html`, setup modal |

`mockup-analytics.html` is dropped.

**Sync UX:** a "Sync" button in the header opens a quiet panel: "Approve the
login in your Trade Republic app" with a 120 s countdown. htmx polls
`/sync/<id>/` until the run finishes, then the page refreshes its data.

## Phases

Each phase ends with something runnable.

0. **Baseline** (done)
   - The old repo, including `.git`, the uncommitted MFA work and `DATA/`, is archived at `~/backups/trade-republic-dashboard-legacy-2026-11-05.tar.gz`.
   - Legacy code moved to a gitignored `legacy/` folder for reference and the parity test.
   - Fresh `git init`; the old remote (`cdamken/trade-republic-dashboard`) is removed.
1. **Skeleton**
   - `pyproject.toml`, Django project, the apps, SQLite (WAL), models and migrations, admin.
   - Verify: `manage.py check` and the admin show the empty tables.
2. **Broker port**
   - `broker/tr.py`, `broker/mapping.py` (with exceptions), `sync.py`, `manage.py sync --full`.
   - Verify with a real approve-in-app login and a full sync. Per-kind counts must match today's CSV (1,787 rows).
   - Also investigate why the last sync returned **0 positions** (cash only).
3. **Worker + sync UX**
   - `SyncRun` queue, keepalive loop, status endpoint, the header sync panel.
   - Verify: Sync from the browser, approve on phone, run reaches `done`.
4. **Analytics**
   - Port to `portfolio/analytics/` with unit tests, plus the parity test against current `analytics.json`.
5. **Design system + pages**
   - Tokens, base template, components (KPI, table, chart wrapper).
   - Then the pages in order: Overview → Positions (+ detail) → Activity → Income → Performance → Settings.
6. **Extras**
   - Documents download, CSV exports, `timelineDetailV2` enrichment (shares, fees, taxes), `reclassify`.
7. **Cleanup**
   - Delete `legacy/` and every other unused file.
   - New `run.sh` (start/stop web + worker).
   - Rewrite `README.md` and `CLAUDE.md`, removing the ownCloud-upstream workflow; triage `BACKLOG.md`.

Migration of existing data: none needed. A fresh full sync rebuilds
everything, with TR ids and raw payloads that the old CSV lacks. Credentials
are read once from `~/.pytr/credentials` on first run; the existing tr-api
cookies keep working.

## License

The original repo and `tr-api` are **BSL-1.1 © Carlos Damken**: personal,
non-commercial use only. This rework is for personal use only; the kept TR
code stays under that license. Do not publish or host it.
