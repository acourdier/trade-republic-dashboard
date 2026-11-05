from django.urls import path

from ui import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("positions/", views.positions, name="positions"),
    path("positions/<str:isin>/", views.position, name="position"),
    path("activity/", views.activity, name="activity"),
    path("income/", views.income, name="income"),
    path("performance/", views.performance, name="performance"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/reset/", views.reset, name="reset"),
    path("documents/<int:pk>/", views.document, name="document"),
    path("sync/", views.sync_start, name="sync_start"),
    path("sync/<int:pk>/", views.sync_status, name="sync_status"),
]
