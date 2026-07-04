from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from apps.ingestion.connectors import (
    CONNECTORS,
    BaseConnector,
    ConnectorFetchError,
    ExaConnector,
    SciBotConnector,
    UnpaywallConnector,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sources"


@pytest.mark.parametrize("source_key", sorted(CONNECTORS.keys()))
def test_source_fixture_parsing(source_key: str) -> None:
    """Test source fixture parsing helper."""
    connector_cls = CONNECTORS[source_key]
    connector: BaseConnector = connector_cls()
    profile = connector.profile
    query = "AI in neurobiology" if source_key == "scibot" else "machine learning"

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


def test_unpaywall_email_falls_back_to_cindex_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unpaywall must use the local fallback email when env/git are absent."""

    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": ""})(),
    )

    assert UnpaywallConnector._email() == "cindex@app.local"


def test_scibot_altcha_solver_roundtrip() -> None:
    """SciBot ALTCHA solver should reproduce the expected digest payload."""

    challenge = {
        "algorithm": "SHA-256",
        "challenge": "84e2bd15202dafc6d25ae7abe1e18fce8f7ce0e5860f5c0ec14f3ee907ab05dc",
        "maxnumber": 500000,
        "salt": "815112f962504c789cf850e7",
        "signature": "b879f5fa5969bd20752fa369ef094cebd05f5da9763a6fcd723691d66d4fde79",
    }

    payload = SciBotConnector._solve_altcha_challenge(challenge)
    decoded = json.loads(base64.b64decode(payload.encode("ascii")).decode("utf-8"))

    assert decoded == {
        "algorithm": challenge["algorithm"],
        "challenge": challenge["challenge"],
        "number": 216985,
        "salt": challenge["salt"],
        "signature": challenge["signature"],
    }


def test_scibot_ignores_prose_without_structured_article_cards() -> None:
    """SciBot should not synthesize article records from prose-only streams."""

    connector = SciBotConnector()
    messages = [
        {
            "type": "content",
            "text": "Now let me get a few more articles on specific topics.",
        },
        {"type": "content", "text": "I'll continue searching and summarizing."},
        {"type": "done"},
    ]

    items = connector._extract_from_ws_messages("AI in neurobiology", messages, limit=3)

    assert items == []


def test_scibot_keeps_structured_cards_even_for_unrelated_query() -> None:
    """SciBot should accept structured article cards regardless of query tokens."""

    connector = SciBotConnector()
    messages = [
        {
            "type": "tool_end",
            "tool": "read_article",
            "article": {
                "title": (
                    "Impact of ChatGPT and Artificial Intelligence in the "
                    "Contemporary Medical Landscape"
                ),
                "doi": "10.1016/j.arcmed.2023.05.003",
                "journal": "Archives of Medical Research",
                "year": 2023,
                "abstract": (
                    "<jats:title>Abstract</jats:title><jats:p>Artificial "
                    "intelligence is transforming medicine.</jats:p>"
                ),
            },
            "doi": "10.1016/j.arcmed.2023.05.003",
        }
    ]

    items = connector._extract_from_ws_messages(
        "архитектура римских дорог",
        messages,
        limit=3,
    )

    assert len(items) == 1
    assert items[0].title.startswith("Impact of ChatGPT")
    assert items[0].doi == "10.1016/j.arcmed.2023.05.003"


def test_exa_search_payload_parsing() -> None:
    """Exa payload parsing should yield article-like records."""

    connector = ExaConnector()
    payload = json.loads((FIXTURES_DIR / "exa.json").read_text(encoding="utf-8"))
    items = connector._extract_from_payload("machine learning", payload, limit=3)

    assert len(items) == 2
    assert items[0].source_key == "exa"
    assert items[0].title.startswith("Artificial intelligence in neurobiology")
    assert items[0].url.startswith("http")
