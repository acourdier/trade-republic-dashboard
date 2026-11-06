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
