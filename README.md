# Trade Republic dashboard

A single-user dashboard for one Trade Republic account: positions,
per-instrument history, activity, income, performance against index funds.
Django 6.1 + SQLite; runs on your computer or your own server (Docker).

**Personal use only.** It uses [`tr-api`](https://github.com/cdamken/tr-api)
and code derived from Carlos Damken's dashboard, both BSL-1.1 (see LICENSE).
Do not publish it or host it for anyone else.

## Run locally

Needs [uv](https://docs.astral.sh/uv/).

```sh
uv sync && uv run playwright install chromium   # once
uv run python manage.py serve                    # migrates, then serves :8085 + the sync worker
uv run python manage.py sync                     # one sync in the terminal (--full, --documents)
```

Open <http://localhost:8085/>, go to **Settings**, enter your phone number
(+49…) and PIN, then press **Sync**. Trade Republic sends a login request to
your phone; approve it in the app within two minutes. Later syncs reuse the
session while it is alive (the app refreshes it every ~5 minutes while running).

## Docker (your own server)

```sh
cp .env.example .env        # set TRDASH_PASSWORD and TRDASH_ALLOWED_HOSTS
docker compose up -d --build
docker compose logs -f
```

- One container runs the web app, the sync worker and the session keepalive.
  Keep it at **one replica**: SQLite and the TR session are single-writer.
- All state is in `./data` (mounted at `/data`), including tr-api's profile
  (`data/home/.tr-api`). Back up that folder. To move your local install to the
  server, copy `data/` across before the first start. The server then logs in
  as a new device, so approve it in the TR app on the first sync.
- The port is published on **127.0.0.1:8085 only**. Put a TLS reverse proxy in
  front and set `TRDASH_BEHIND_PROXY=1`, for example with Caddy:
  `tr.example.com { reverse_proxy 127.0.0.1:8085 }`. Or reach it through a VPN
  or SSH tunnel (`ssh -L 8085:127.0.0.1:8085 server`).
- `TRDASH_PASSWORD` is required in Docker: the sign-in password for the dashboard,
  which holds your TR PIN. Changing it signs every browser out.
- One-off commands run as the app user through the entrypoint:
  `docker compose exec trdash manage sync --full`, `… manage reclassify`.
- Health check: `GET /healthz`. The image is about 1.3 GB, mostly the headless
  Chromium that tr-api needs to get Trade Republic's bot-protection token at
  login.
- When TR redeploys its web app and logins fail, set `TR_WEB_APP_VERSION` in
  `.env` and run `docker compose up -d`.

## Where things live

| Path | What |
|---|---|
| `data/trdash.sqlite3` | All synced data (copy this file to back up) |
| `data/secrets/` | Phone + PIN, Django secret key (0600, never committed) |
| `data/documents/` | Downloaded PDFs |
| `data/logs/` | `trdash.log` (also on stdout) |
| `~/.tr-api/profiles/<phone>/` | Trade Republic session cookies (managed by tr-api) |

Environment overrides: `TRDASH_HOST`, `TRDASH_PORT`, `TRDASH_DATA_DIR`,
`TRDASH_PASSWORD`, `TRDASH_ALLOWED_HOSTS`, `TRDASH_CSRF_TRUSTED_ORIGINS`,
`TRDASH_BEHIND_PROXY`, `TRDASH_TIME_ZONE`, `TR_WEB_APP_VERSION`, `TRDASH_DEBUG=1`.
See `.env.example`.

## Development

```sh
uv run pytest
uv run ruff check .
uv run python manage.py reclassify   # re-map stored events after a mapping change
```
