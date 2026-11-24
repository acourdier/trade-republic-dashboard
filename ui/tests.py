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


@pytest.mark.django_db
def test_url_prefix_behind_proxy(client, settings, request):
    """nginx serves /trdash/ and strips the prefix; generated URLs keep it."""
    from django.urls import set_script_prefix

    settings.FORCE_SCRIPT_NAME = "/trdash"
    settings.STATIC_URL = "/trdash/static/"
    settings.TRDASH_PASSWORD = "s3cret"
    # WSGIHandler does this per request; the test client doesn't.
    set_script_prefix("/trdash/")
    request.addfinalizer(lambda: set_script_prefix("/"))
    r = client.get("/activity/")
    assert r["Location"] == "/trdash/login/?next=%2Ftrdash%2Factivity%2F"
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/ui/css/app.css").status_code == 200
    r = client.post("/login/", {"password": "s3cret", "next": "/trdash/activity/"})
    assert r["Location"] == "/trdash/activity/"
    page = client.get("/settings/").content.decode()
    assert 'data-url="/trdash/sync/"' in page
    assert "/trdash/static/ui/js/app.js" in page


@pytest.mark.django_db
def test_projection_page(client, settings):
    from datetime import date, timedelta
    from decimal import Decimal

    from broker.models import Setting
    from portfolio.models import ValuePoint

    settings.TRDASH_PASSWORD = ""
    r = client.get("/projection/")
    assert r.status_code == 200 and b"Run one to see the projection" in r.content

    start = date(2023, 5, 31)
    ValuePoint.objects.bulk_create(
        ValuePoint(date=start + timedelta(days=i), cash=Decimal(100), depot_value=Decimal(900 + i),
                   net_capital_in=Decimal(1000), cost_basis=Decimal(900), source="rebuilt")
        for i in range(40))
    for q in ("", "?view=real", "?base=today"):
        r = client.get(f"/projection/{q}")
        assert r.status_code == 200
        assert b'id="proj-data"' in r.content and b"May 2053" in r.content

    form = {"monthly_contribution": "900", "contribution_growth_pct": "2", "pessimistic_pct": "2",
            "baseline_pct": "5", "optimistic_pct": "7", "inflation_pct": "2",
            "cash_rate_pct": "", "horizon_years": "30"}
    r = client.post("/projection/?view=real", form)
    assert r.status_code == 302 and r["Location"] == "/projection/?view=real"
    assert Setting.get("projection")["monthly_contribution"] == 900.0
    r = client.post("/projection/", {**form, "baseline_pct": "lots"})
    assert r.status_code == 200 and b"enter a number" in r.content
    assert Setting.get("projection")["baseline_pct"] == 5.0


@pytest.mark.django_db
def test_projection_is_gated(client, settings):
    settings.TRDASH_PASSWORD = "s3cret"
    assert client.get("/projection/").status_code == 302
