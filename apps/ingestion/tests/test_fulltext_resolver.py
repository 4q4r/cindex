from __future__ import annotations

from types import SimpleNamespace

from apps.ingestion.fulltext_resolver import LawfulFullTextResolver


class _DummyResponse:
    def __init__(self, content: bytes, content_type: str) -> None:
        self.content = content
        self.headers = {"Content-Type": content_type}


class _DummyConnector:
    def _request_response(self, url: str, params=None, accept=None):
        if url == "https://example.org/full.pdf":
            pdf_bytes = b"%PDF-1.4 dummy"
            return (
                None,
                _DummyResponse(pdf_bytes, "application/pdf"),
                pdf_bytes.decode("latin-1"),
            )
        msg = f"unexpected url {url}"
        raise AssertionError(msg)

    def _is_pdf_response(self, url: str, content_type: str, body: bytes) -> bool:
        return url.endswith(".pdf") or "application/pdf" in content_type

    def _extract_pdf_text(self, pdf_bytes: bytes) -> str:
        return "PDF body text"

    def _extract_pdf_text_with_language(
        self, pdf_bytes: bytes, *, ocr_language: str = "eng"
    ) -> str:
        return self._extract_pdf_text(pdf_bytes)

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
