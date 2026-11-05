# Trade Republic dashboard

A local, single-user dashboard for one Trade Republic account: positions,
per-instrument history, activity, income, performance against index funds.
Django 6.1 + SQLite; everything stays on this computer.

**Personal use only.** It uses [`tr-api`](https://github.com/cdamken/tr-api)
and code derived from Carlos Damken's dashboard, both BSL-1.1 (see LICENSE).
Do not publish or host it.

## Run

Needs [uv](https://docs.astral.sh/uv/).

```sh
./run.sh            # first run installs deps + Chromium, migrates, starts on :8085
./run.sh stop | restart | status
./run.sh sync       # one sync in the terminal (add --full or --documents)
```

Open <http://localhost:8085/>, go to **Settings**, enter your phone number
(+49…) and PIN, then press **Sync**. Trade Republic sends a login request to
your phone; approve it in the app within two minutes. Later syncs reuse the
session while it is alive (the app refreshes it every ~5 minutes while running).

## Where things live

| Path | What |
|---|---|
| `data/trdash.sqlite3` | All synced data (copy this file to back up) |
| `data/secrets/` | Phone + PIN, Django secret key (0600, never committed) |
| `data/documents/` | Downloaded PDFs |
| `data/logs/` | `server.log`, `trdash.log` |
| `~/.tr-api/profiles/<phone>/` | Trade Republic session cookies (managed by tr-api) |

Environment overrides: `TRDASH_PORT`, `TRDASH_DATA_DIR`, `TRDASH_TIME_ZONE`,
`TR_WEB_APP_VERSION`, `TRDASH_DEBUG=1`.

## Development

```sh
uv run pytest
uv run ruff check .
uv run python manage.py reclassify   # re-map stored events after a mapping change
```
