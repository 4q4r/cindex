from __future__ import annotations

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from apps.ingestion.connectors import (
    CONNECTORS,
    BaseConnector,
    ConnectorFetchError,
    ExaConnector,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sources"


@pytest.mark.parametrize("source_key", sorted(CONNECTORS.keys()))
def test_source_fixture_parsing(source_key: str) -> None:
    """Test source fixture parsing helper."""
    connector_cls = CONNECTORS[source_key]
    connector: BaseConnector = connector_cls()
    profile = connector.profile
    query = "machine learning"

    if profile.mode == "api":
        payload_path = FIXTURES_DIR / f"{source_key}.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        items = connector._extract_from_payload(query, payload, limit=3)
    elif profile.mode == "ws":
        payload_path = FIXTURES_DIR / f"{source_key}.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        items = connector._extract_from_ws_messages(
            query,
            payload,
            limit=3,
        )
    else:
        html_path = FIXTURES_DIR / f"{source_key}.html"
        html = html_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "lxml")
        items = connector._extract_from_html(query, soup, limit=3)

    assert items, f"Expected parsed items for source {source_key}"
    assert items[0].source_key == source_key
    assert items[0].title
    assert items[0].url.startswith("http")


def test_connector_detects_verification_page() -> None:
    """Test connector detects verification page helper."""
    connector = CONNECTORS["dergipark"]()
    html = (FIXTURES_DIR / "blocked_verification.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    with pytest.raises(ConnectorFetchError):
        connector._assert_page_is_parseable(html, soup)


def test_exa_search_payload_parsing() -> None:
    """Exa payload parsing should yield article-like records."""
    connector = ExaConnector()
    payload = json.loads((FIXTURES_DIR / "exa.json").read_text(encoding="utf-8"))
    items = connector._extract_from_payload("machine learning", payload, limit=3)

    assert len(items) == 2
    assert items[0].source_key == "exa"
    assert items[0].title.startswith("Artificial intelligence in neurobiology")
    assert items[0].url.startswith("http")
