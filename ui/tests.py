import pytest
from django.test import Client


@pytest.fixture
def client():
    return Client(HTTP_HOST="localhost")


@pytest.mark.django_db
def test_open_without_password(client, settings):
    settings.TRDASH_PASSWORD = ""
    assert client.get("/settings/").status_code == 200
    assert client.get("/login/").status_code == 302


@pytest.mark.django_db
def test_gate_redirects_and_unlocks(client, settings):
    settings.TRDASH_PASSWORD = "s3cret"
    r = client.get("/activity/?q=x")
    assert r.status_code == 302
    assert r["Location"] == "/login/?next=%2Factivity%2F%3Fq%3Dx"
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/ui/css/app.css").status_code == 200

    assert client.post("/login/", {"password": "nope"}).status_code == 401
    r = client.post("/login/", {"password": "s3cret", "next": "/activity/"})
    assert r.status_code == 302 and r["Location"] == "/activity/"
    assert client.get("/settings/").status_code == 200

    # Changing the password signs existing sessions out.
    settings.TRDASH_PASSWORD = "other"
    assert client.get("/settings/").status_code == 302


@pytest.mark.django_db
def test_gate_rejects_offsite_next_and_json(client, settings):
    settings.TRDASH_PASSWORD = "s3cret"
    r = client.post("/login/", {"password": "s3cret", "next": "https://evil.example/"})
    assert r["Location"] == "/"
    client.post("/logout/")
    r = client.get("/sync/1/", HTTP_ACCEPT="application/json")
    assert r.status_code == 401


@pytest.mark.django_db
def test_allowed_hosts_from_env(settings):
    settings.ALLOWED_HOSTS = ["localhost", "tr.example.com"]
    assert Client(HTTP_HOST="tr.example.com").get("/healthz").status_code == 200
    assert Client(HTTP_HOST="evil.example").get("/healthz").status_code == 400


def test_https_origins_for_allowed_hosts():
    from trdash.settings import https_origins

    assert https_origins(["box.tail1234.ts.net", ".example.com", "*"]) == [
        "https://box.tail1234.ts.net",
        "https://*.example.com",
    ]


def _csrf_post(settings, origin, **extra):
    settings.TRDASH_PASSWORD = "s3cret"
    settings.ALLOWED_HOSTS = ["box.tail1234.ts.net"]
    c = Client(enforce_csrf_checks=True, HTTP_HOST="box.tail1234.ts.net")
    token = c.get("/login/").context["csrf_token"]
    return c.post("/login/", {"csrfmiddlewaretoken": str(token), "password": "s3cret"},
                  HTTP_ORIGIN=origin, **extra)


@pytest.mark.django_db
def test_login_through_tls_proxy_without_behind_proxy(settings):
    # `tailscale serve`: browser on https, app sees http, TRDASH_BEHIND_PROXY unset.
    settings.CSRF_TRUSTED_ORIGINS = ["https://box.tail1234.ts.net"]
    assert _csrf_post(settings, "https://box.tail1234.ts.net").status_code == 302


@pytest.mark.django_db
def test_csrf_failure_explains_proxy_mismatch(settings):
    settings.CSRF_TRUSTED_ORIGINS = []
    r = _csrf_post(settings, "https://box.tail1234.ts.net")
    assert r.status_code == 403
    assert b"Origin checking failed" in r.content
    assert b"TRDASH_BEHIND_PROXY=1" in r.content


@pytest.mark.django_db
def test_csrf_failure_explains_secure_cookie_over_http(settings):
    settings.TRDASH_PASSWORD = "s3cret"
    settings.CSRF_COOKIE_SECURE = True
    c = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
    r = c.post("/login/", {"password": "s3cret"})  # browser dropped the Secure cookie
    assert r.status_code == 403
    assert b"CSRF cookie not set" in r.content
    assert b"https://" in r.content
