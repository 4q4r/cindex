"""Tests for native/OCR PDF extraction and the /pdf-text contract."""

from __future__ import annotations

import base64

import pymupdf
from fastapi.testclient import TestClient

from cindex_browser_sidecar import main
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
