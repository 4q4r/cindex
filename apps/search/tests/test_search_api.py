from rest_framework.test import APIClient


def test_search_endpoint_returns_payload(db) -> None:
    """Test search endpoint returns payload helper."""
    client = APIClient()
    response = client.post("/api/v1/search", {"query": "deep learning"}, format="json")
    assert response.status_code == 200
    assert "results" in response.data
    assert "source_stats" in response.data


def test_search_endpoint_passes_expression(monkeypatch, db) -> None:
    """Test search endpoint passes expression helper."""
    captured: dict = {}

    def fake_run(query, expression, force_refresh=False, filters=None):
        captured["query"] = query
        captured["expression"] = expression
        captured["force_refresh"] = force_refresh
        captured["filters"] = filters
        return []

    monkeypatch.setattr("apps.search.views.SearchService.run", fake_run)
    client = APIClient()
    response = client.post(
        "/api/v1/search",
        {
            "query": "machine learning diagnosis",
            "expression": '"machine learning" AND diagnosis -survey',
            "peer_reviewed_only": True,
            "year_from": 2010,
            "sort_by": "newest",
            "per_page": 3,
        },
        format="json",
    )
    assert response.status_code == 200
    assert captured["query"] == "machine learning diagnosis"
    assert captured["expression"] == '"machine learning" AND diagnosis -survey'
    assert captured["filters"].peer_reviewed_only is True
    assert captured["filters"].year_from == 2010
    assert captured["filters"].normalized_sort() == "newest"
    assert response.data["per_page"] == 3
    assert response.data["total_pages"] == 0
    assert response.data["count"] == 0


def test_source_stats_endpoint_returns_totals(db) -> None:
    """Test source stats endpoint returns totals helper."""
    client = APIClient()
    response = client.get("/api/v1/source-stats")
    assert response.status_code == 200
    assert "total" in response.data
    assert "live" in response.data
    assert "failed" in response.data
