from __future__ import annotations

from types import SimpleNamespace

from apps.ingestion.connectors.base import FetchResult
from apps.ingestion.fulltext_resolver import LawfulFullTextResolver


class _DummyTransport:
    """Stand-in for ``BrowserTransport`` returning canned PDF payloads."""

    def fetch(self, url, *, params=None, accept=None, timeout=None) -> FetchResult:
        if url == "https://example.org/full.pdf":
            return FetchResult(
                status=200,
                content_type="application/pdf",
                body_bytes=b"%PDF-1.4 dummy",
                body_text=None,
            )
        msg = f"unexpected url {url}"
        raise AssertionError(msg)

    def post_form(self, *args, **kwargs) -> FetchResult:
        msg = "post_form not expected"
        raise AssertionError(msg)

    def post_json(self, *args, **kwargs) -> FetchResult:
        msg = "post_json not expected"
        raise AssertionError(msg)


class _DummyConnector:
    def __init__(self) -> None:
        self._transport = _DummyTransport()

    def _is_pdf_response(self, url, content_type, body) -> bool:
        return url.endswith(".pdf") or "application/pdf" in content_type

    def _extract_pdf_text_with_language(
        self,
        pdf_bytes,
        *,
        ocr_language: str = "eng",
    ) -> str:
        return "PDF body text"

    def _extract_pdf_url(self, soup, raw_url, raw_full_text, combined_page_text) -> str:
        return ""

    def _request_pdf_text(self, url: str) -> str:
        return "PDF body text"


def test_lawful_resolver_prefers_unpaywall_pdf(monkeypatch) -> None:
    """Resolver should augment text from an OA PDF URL discovered via Unpaywall."""

    async def _fake_unpaywall(self, doi):
        return ["https://example.org/full.pdf"]

    async def _fake_epmc(self, doi):
        return []

    monkeypatch.setattr(
        LawfulFullTextResolver,
        "_unpaywall_candidate_urls_async",
        _fake_unpaywall,
    )
    monkeypatch.setattr(
        LawfulFullTextResolver,
        "_europe_pmc_candidate_urls_async",
        _fake_epmc,
    )

    resolver = LawfulFullTextResolver(_DummyConnector())
    raw = SimpleNamespace(doi="10.1234/example.1")
    text = resolver.resolve(raw, existing_text="Abstract text")

    assert "Abstract text" in text
    assert "PDF body text" in text
