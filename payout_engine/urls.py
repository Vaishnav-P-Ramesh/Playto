"""Root URL configuration for payout_engine."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("payouts.urls")),
]
