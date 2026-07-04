from django.urls import path

from .views import (
    ReindexView,
    SearchJobCreateView,
    SearchJobDetailView,
    SearchView,
    SourceStatsView,
)

urlpatterns = [
    path("source-stats", SourceStatsView.as_view(), name="source-stats"),
    path("search/jobs", SearchJobCreateView.as_view(), name="search-jobs-create"),
    path(
        "search/jobs/<uuid:job_id>",
        SearchJobDetailView.as_view(),
        name="search-jobs-detail",
    ),
    path("search", SearchView.as_view(), name="search"),
    path("admin/reindex", ReindexView.as_view(), name="reindex"),
]
