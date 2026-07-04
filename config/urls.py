"""Root URL configuration for the CIndex project."""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.healthcheck import healthcheck_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", healthcheck_view),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/v1/", include("apps.search.urls")),
]
