from __future__ import annotations

from types import SimpleNamespace

from apps.ingestion.connectors.base import (
    ConnectorFetchError,
    FetchResult,
)
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


class _FailingTransport:
    """Transport whose ``fetch`` always raises ``ConnectorFetchError``.

    Models the browser sidecar returning 502 (e.g. a PDF URL the sidecar
    cannot navigate to). The resolver must treat this as best-effort and
    fall back to the existing text instead of propagating the error and
    aborting the whole ``enrich_raw`` call.
    """

    def fetch(self, url, *, params=None, accept=None, timeout=None) -> FetchResult:
        msg = "ajol: browser sidecar returned 502 after retries"
        raise ConnectorFetchError(msg)

    def post_form(self, *args, **kwargs) -> FetchResult:
        msg = "post_form not expected"
        raise AssertionError(msg)

    def post_json(self, *args, **kwargs) -> FetchResult:
        msg = "post_json not expected"
        raise AssertionError(msg)


class _FailingConnector(_DummyConnector):
    def __init__(self) -> None:
        super().__init__()
        self._transport = _FailingTransport()


def test_lawful_resolver_swallows_connector_fetch_error(monkeypatch) -> None:
    """A sidecar 502 during full-text fetch must not abort enrichment.

    ``_fetch_url_text`` calls the connector transport, which raises
    ``ConnectorFetchError`` when the browser sidecar returns a non-retryable
    error (502). The resolver catches it and returns the existing text so
    the already-extracted abstract/metadata survive.
    """

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

    resolver = LawfulFullTextResolver(_FailingConnector())
    raw = SimpleNamespace(doi="10.1234/example.1")
    text = resolver.resolve(raw, existing_text="Abstract text")

    assert text == "Abstract text"
