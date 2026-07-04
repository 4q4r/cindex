from rest_framework.test import APIClient


def test_search_endpoint_returns_payload(db):
    """Test search endpoint returns payload helper."""
    client = APIClient()
    response = client.post("/api/v1/search", {"query": "deep learning"}, format="json")
    assert response.status_code == 200
    assert "results" in response.data
    assert "source_stats" in response.data


def test_search_endpoint_passes_expression(monkeypatch, db):
    """Test search endpoint passes expression helper."""
    captured: dict = {}

    def fake_run(query: str, expression: str, force_refresh: bool = False):
        captured["query"] = query
        captured["expression"] = expression
        captured["force_refresh"] = force_refresh
        return []

    monkeypatch.setattr("apps.search.views.SearchService.run", fake_run)
    client = APIClient()
    response = client.post(
        "/api/v1/search",
        {
            "query": "machine learning diagnosis",
            "expression": '"machine learning" AND diagnosis -survey',
        },
        format="json",
    )
    assert response.status_code == 200
    assert captured["query"] == "machine learning diagnosis"
    assert captured["expression"] == '"machine learning" AND diagnosis -survey'


def test_source_stats_endpoint_returns_totals(db):
    """Test source stats endpoint returns totals helper."""
    client = APIClient()
    response = client.get("/api/v1/source-stats")
    assert response.status_code == 200
    assert "total" in response.data
    assert "live" in response.data
    assert "failed" in response.data
