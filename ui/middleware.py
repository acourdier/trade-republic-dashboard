"""Single-password gate for server deployments.

Off when TRDASH_PASSWORD is unset (the local, loopback-only setup). When set,
every page except the login form, static files and /healthz needs a session
that was unlocked with the current password. The session stores a keyed hash of
the password, so changing TRDASH_PASSWORD signs everyone out.
"""

import hashlib
import hmac

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode

SESSION_KEY = "trdash_auth"
OPEN_PREFIXES = ("/login/", "/healthz", "/static/")


def password_fingerprint() -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), settings.TRDASH_PASSWORD.encode(), hashlib.sha256
    ).hexdigest()


def check_password(candidate: str) -> bool:
    return bool(settings.TRDASH_PASSWORD) and hmac.compare_digest(
        candidate.encode(), settings.TRDASH_PASSWORD.encode()
    )


def is_unlocked(request) -> bool:
    if not settings.TRDASH_PASSWORD:
        return True
    stored = request.session.get(SESSION_KEY, "")
    return hmac.compare_digest(stored, password_fingerprint())


class PasswordGate:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if is_unlocked(request) or request.path.startswith(OPEN_PREFIXES):
            return self.get_response(request)
        if request.headers.get("Accept", "").startswith("application/json"):
            return JsonResponse({"message": "Signed out. Reload the page."}, status=401)
        return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
