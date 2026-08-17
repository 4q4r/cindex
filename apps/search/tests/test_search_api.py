from rest_framework.test import APIClient

from apps.search.serializers import SearchResultSerializer


def _minimal_result(**overrides: object) -> dict:
    """A result dict with every required ``SearchResultSerializer`` field."""
    result = {
        "id": 1,
        "title": "A Study",
        "preview": "Abstract.",
        "year": 2024,
        "publication_date": None,
        "source": "test",
        "journal": "Journal",
        "authors": ["A. Author"],
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "10.1/1",
        "eligibility_evidence": {"not_retracted": True},
        "eligibility_confidence": {"not_retracted": 0.9},
        "is_retracted": False,
        "retraction_note": "",
        "cited_by_count": 3,
        "tier": "A",
        "url": "https://example.org/1",
    }
    result.update(overrides)
    return result


class TestSearchResultSerializerTldrContract:
    """The frontend depends on a stable, always-present ``tldr`` field."""

    def test_tldr_defaults_to_empty_string_when_missing(self) -> None:
        serializer = SearchResultSerializer(data=_minimal_result())
        assert serializer.is_valid(), serializer.errors

        assert serializer.data["tldr"] == ""

    def test_tldr_passthrough_when_present(self) -> None:
        serializer = SearchResultSerializer(
            data=_minimal_result(tldr="Краткое резюме статьи."),
        )
        assert serializer.is_valid(), serializer.errors

        assert serializer.data["tldr"] == "Краткое резюме статьи."

    def test_tldr_whitespace_collapsed_and_truncated_to_500(self) -> None:
        serializer = SearchResultSerializer(
            data=_minimal_result(tldr="  a\n\nb  " + "x" * 600),
        )
        assert serializer.is_valid(), serializer.errors

        assert serializer.data["tldr"] == "a b " + "x" * 496


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
