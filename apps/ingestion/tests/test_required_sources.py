from apps.ingestion.connectors import CONNECTORS
from apps.ingestion.live_queries import REQUIRED_SOURCES

API_ONLY_SOURCES = {
    "openalex",
    "crossref",
    "semantic_scholar",
    "pubmed",
    "arxiv",
}


def test_required_source_registry_complete() -> None:
    """Test required source registry complete helper."""
    active_required_sources = {
        source_key
        for source_key in REQUIRED_SOURCES
        if source_key not in API_ONLY_SOURCES
    }
    assert active_required_sources.issubset(set(CONNECTORS.keys()))
