"""Tests for native/OCR PDF extraction and the /pdf-text contract."""

from __future__ import annotations

import base64

import pymupdf
from fastapi.testclient import TestClient

from cindex_browser_sidecar import main
from cindex_browser_sidecar.pdf_pages import render_pdf_pages
from cindex_browser_sidecar.pdf_text import extract_pdf_text


def _text_pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    payload = document.tobytes()
    document.close()
    return payload


def test_extract_pdf_text_native() -> None:
    text = extract_pdf_text(_text_pdf("Native PDF text"), ocr_language="eng")
    assert text == "Native PDF text"


def test_extract_pdf_text_invalid_pdf_is_empty() -> None:
    assert extract_pdf_text(b"not a PDF", ocr_language="eng") == ""


def test_pdf_text_endpoint(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_extract(body: bytes, *, ocr_language: str) -> str:
        seen.update(body=body, language=ocr_language)
        return "extracted text"

    monkeypatch.setattr(main, "extract_pdf_text", fake_extract)
    client = TestClient(main.app)
    response = client.post(
        "/pdf-text",
        json={
            "body": base64.b64encode(b"%PDF-test").decode("ascii"),
            "ocr_language": "rus",
        },
    )
    client.close()
    assert response.status_code == 200
    assert response.json() == {"text": "extracted text"}
    assert seen == {"body": b"%PDF-test", "language": "rus"}


def test_render_pdf_pages() -> None:
    pages, text = render_pdf_pages(_text_pdf("Vision page"), max_pages=2, dpi=72)
    assert text == "Vision page"
    assert len(pages) == 1
    assert pages[0]["id"] == "page-0"
    assert pages[0]["body"]
    assert pages[0]["width"] > 0
    assert pages[0]["height"] > 0


def test_pdf_pages_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "render_pdf_pages",
        lambda body, *, max_pages, dpi: (
            [{"id": "page-0", "body": "png", "width": 1, "height": 1}],
            "text",
        ),
    )
    client = TestClient(main.app)
    response = client.post(
        "/pdf-pages",
        json={
            "body": base64.b64encode(b"%PDF-test").decode("ascii"),
            "max_pages": 2,
            "dpi": 72,
        },
    )
    client.close()
    assert response.status_code == 200
    assert response.json()["text"] == "text"
    assert response.json()["pages"][0]["id"] == "page-0"


def test_pdf_text_rejects_invalid_base64() -> None:
    client = TestClient(main.app)
    response = client.post(
        "/pdf-text",
        json={"body": "%%%", "ocr_language": "eng"},
    )
    client.close()
    assert response.status_code == 422


def test_pdf_text_rejects_unknown_ocr_language() -> None:
    client = TestClient(main.app)
    response = client.post(
        "/pdf-text",
        json={
            "body": base64.b64encode(b"%PDF-test").decode("ascii"),
            "ocr_language": "$(unsafe)",
        },
    )
    client.close()
    assert response.status_code == 422
