from __future__ import annotations

import pymupdf

from apps.ingestion.connectors import BaseConnector, RawArticle, SourceProfile


def _build_pdf_bytes(text: str) -> bytes:
    """Internal helper for build pdf bytes."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\n", " ")
    )
    stream = f"BT /F1 24 Tf 72 700 Td ({escaped}) Tj ET"
    objects = [
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        "4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream.encode('latin-1'))} >>\nstream\n"
            f"{stream}\nendstream\nendobj\n"
        ),
    ]
    header = "%PDF-1.4\n"
    parts = [header]
    offsets = [0]
    current = len(header.encode("latin-1"))
    for obj in objects:
        offsets.append(current)
        obj_bytes = obj.encode("latin-1")
        parts.append(obj)
        current += len(obj_bytes)
    xref_offset = current
    xref_lines = ["xref\n0 6\n0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref_lines.append(f"{offset:010d} 00000 n \n")
    trailer = f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    return "".join(parts + xref_lines + [trailer]).encode("latin-1")


class PdfConnector(BaseConnector):
    """Pdf source connector."""

    profile = SourceProfile(
        source_key="pdf",
        search_url="https://example.org/search",
    )

    def _request_text(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        ocr_language: str = "eng",
    ) -> str:
        """Serve a canned PDF payload through the real extraction path."""
        pdf_bytes = _build_pdf_bytes("Hello PDF world for scholarly extraction")
        return self._extract_pdf_text_with_language(
            pdf_bytes,
            ocr_language=ocr_language,
        )


class LandingPdfConnector(BaseConnector):
    """Landing Pdf source connector."""

    profile = SourceProfile(
        source_key="landingpdf",
        search_url="https://example.org/search",
    )

    def _request_text(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        ocr_language: str = "eng",
    ) -> str:
        """Serve the landing HTML, or a canned PDF when the URL targets a PDF."""
        if url.endswith(".pdf"):
            return self._extract_pdf_text_with_language(
                _build_pdf_bytes("Landing page resolved to PDF body text"),
                ocr_language=ocr_language,
            )
        return (
            "<html><head>"
            "<meta name='citation_pdf_url' content='https://example.org/article.pdf'>"
            "</head><body>"
            "<a href='https://example.org/article.pdf'>Full Text PDF</a>"
            "<p>Landing metadata only</p>"
            "</body></html>"
        )

    def _request_pdf_text(self, url: str, *, ocr_language: str = "eng") -> str:
        """Serve the canned landing-page PDF payload through extraction."""
        return self._extract_pdf_text_with_language(
            _build_pdf_bytes("Landing page resolved to PDF body text"),
            ocr_language=ocr_language,
        )


class _OcrPage:
    """Dummy PDF page for OCR fallback tests."""

    def get_text(self, *args, **kwargs) -> str:
        if kwargs.get("textpage") is not None:
            return "Hello PDF world for scholarly extraction"
        return (
            '���13(l v} F$��"R�z Tgb� ��ן 7�����s��L��%�� '  # noqa: RUF001
            "��w� s �caLI�v � �]ƊdUv� ���~�i"
        )

    def get_textpage_ocr(self, language: str = "eng"):
        return object()


class _OcrDocument:
    """Dummy PDF document for OCR fallback tests."""

    def __iter__(self):
        return iter([_OcrPage()])


def test_pdf_url_is_extracted_into_plain_text() -> None:
    """Test pdf url is extracted into plain text helper."""
    connector = PdfConnector()

    text = connector._request_text("https://example.org/article.pdf")

    assert "Hello PDF world" in text
    assert "%PDF" not in text
    assert "xref" not in text


def test_pdf_enrichment_switches_from_landing_page_to_pdf() -> None:
    """Test pdf enrichment switches from landing page to pdf helper."""
    connector = LandingPdfConnector()
    raw = RawArticle(
        source_key="landingpdf",
        title="Landing page article",
        url="https://example.org/landing",
        abstract="",
        full_text="Landing metadata only https://example.org/article.pdf",
        language="en",
        year=2024,
        doi="10.9999/landing.1",
        journal="Landing Journal",
    )

    enriched = connector.enrich_raw(raw)

    assert "Landing page resolved to PDF body text" in enriched.full_text
    assert "Landing metadata only" not in enriched.full_text
    assert "%PDF" not in enriched.full_text


def test_pdf_article_enrichment_uses_extracted_text() -> None:
    """Test pdf article enrichment uses extracted text helper."""
    connector = PdfConnector()
    raw = RawArticle(
        source_key="pdf",
        title="PDF article",
        url="https://example.org/article.pdf",
        abstract="",
        full_text="Title placeholder",
        language="en",
        year=2024,
        doi="10.9999/pdf.1",
        journal="PDF Journal",
    )

    enriched = connector.enrich_raw(raw)

    assert "Hello PDF world" in enriched.full_text
    assert "%PDF" not in enriched.full_text
    assert enriched.abstract == ""


def test_pdf_extraction_uses_ocr_when_native_text_is_garbled(monkeypatch) -> None:
    """Garbled native PDF text should trigger OCR fallback."""
    monkeypatch.setattr(pymupdf, "open", lambda *args, **kwargs: _OcrDocument())
    connector = PdfConnector()

    text = connector._request_text("https://example.org/article.pdf")

    assert "Hello PDF world for scholarly extraction" in text
    assert "���" not in text
