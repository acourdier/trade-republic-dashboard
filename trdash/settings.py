"""Django settings for trdash — a single-user Trade Republic dashboard.

All runtime state lives under DATA_DIR (default ./data, override with
TRDASH_DATA_DIR): the SQLite database, secrets, downloaded documents, logs.
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRDASH_DATA_DIR", BASE_DIR / "data")).resolve()
SECRETS_DIR = DATA_DIR / "secrets"
DOCUMENTS_DIR = DATA_DIR / "documents"
LOG_DIR = DATA_DIR / "logs"
for _d in (DATA_DIR, SECRETS_DIR, DOCUMENTS_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
SECRETS_DIR.chmod(0o700)


def _secret_key() -> str:
    """Generated once per installation, kept out of the repo."""
    path = SECRETS_DIR / "django_secret_key"
    if not path.is_file():
        path.write_text(secrets.token_urlsafe(50), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


SECRET_KEY = os.environ.get("TRDASH_SECRET_KEY") or _secret_key()
DEBUG = os.environ.get("TRDASH_DEBUG", "0") == "1"


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


# Local by default: the server binds 127.0.0.1. For a server deployment set
# TRDASH_HOST=0.0.0.0 plus the public hostname(s) and origin(s) below.
HOST = os.environ.get("TRDASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("TRDASH_PORT", "8085"))
def https_origins(hosts: list[str]) -> list[str]:
    """A TLS proxy (Caddy, `tailscale serve`) makes the browser send Origin
    https://host while the app may see plain http. The hosts are ours, so their
    https origin is trusted even without TRDASH_BEHIND_PROXY. A leading "." in
    ALLOWED_HOSTS means subdomains; "*" is skipped."""
    return [f"https://*{h}" if h.startswith(".") else f"https://{h}" for h in hosts if h != "*"]


ALLOWED_HOSTS = ["127.0.0.1", "localhost", *_env_list("TRDASH_ALLOWED_HOSTS")]
CSRF_TRUSTED_ORIGINS = [
    f"http://localhost:{PORT}",
    f"http://127.0.0.1:{PORT}",
    *https_origins(_env_list("TRDASH_ALLOWED_HOSTS")),
    *_env_list("TRDASH_CSRF_TRUSTED_ORIGINS"),  # e.g. https://tr.example.com:8443
]
CSRF_FAILURE_VIEW = "ui.views.csrf_failure"

# Behind a TLS-terminating reverse proxy (Caddy, nginx, Traefik).
# The proxy must be the only thing that can reach the port.
BEHIND_PROXY = os.environ.get("TRDASH_BEHIND_PROXY", "0") == "1"
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True

# Password gate. Unset = open (fine on loopback); always set it on a server.
TRDASH_PASSWORD = os.environ.get("TRDASH_PASSWORD", "")
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"  # no DB table
SESSION_COOKIE_AGE = 30 * 86400
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "broker",
    "portfolio",
    "ui",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "ui.middleware.PasswordGate",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "trdash.urls"
WSGI_APPLICATION = "trdash.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "ui.context_processors.chrome",
            ],
        },
    },
]

# SQLite in WAL mode: the web process and the worker both write. IMMEDIATE
# transactions + busy_timeout make concurrent writers wait instead of failing.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "trdash.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA foreign_keys=ON;"
            ),
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.environ.get("TRDASH_TIME_ZONE") or "Europe/Berlin"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"

# Trade Republic web-frontend build sent as X-TR-App-Version during login.
# Bump when TR redeploys app.traderepublic.com (see broker/tr.py).
TR_WEB_APP_VERSION = os.environ.get("TR_WEB_APP_VERSION") or "2.2645.8"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "trdash.log",
            "maxBytes": 2_000_000,
            "backupCount": 3,
            "formatter": "plain",
        },
    },
    "loggers": {
        "trdash": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
