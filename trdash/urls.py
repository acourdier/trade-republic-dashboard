from django.contrib.staticfiles.views import serve as static_serve
from django.urls import include, path, re_path

urlpatterns = [
    path("", include("ui.urls")),
    # Local single-user app: serve static files directly, no collectstatic step.
    re_path(r"^static/(?P<path>.*)$", static_serve, {"insecure": True}),
]
