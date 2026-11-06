# Trade Republic dashboard: Django + waitress + the sync worker in one process,
# Chromium for tr-api's WAF token. All state lives in the /data volume.

FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

# ---------------------------------------------------------------- dependencies
FROM python:3.13-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# ---------------------------------------------------------------- runtime
FROM python:3.13-slim-bookworm
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TRDASH_DATA_DIR=/data \
    HOME=/data/home \
    TRDASH_HOST=0.0.0.0 \
    TRDASH_PORT=8085
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Headless Chromium + its system libraries, matching the locked Playwright.
RUN playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin app
COPY docker/entrypoint.sh /usr/local/bin/manage
RUN chmod 755 /usr/local/bin/manage
COPY manage.py ./
COPY trdash trdash
COPY broker broker
COPY portfolio portfolio
COPY ui ui

VOLUME /data
EXPOSE 8085
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/healthz', timeout=4)"]
ENTRYPOINT ["manage"]
CMD ["serve"]
