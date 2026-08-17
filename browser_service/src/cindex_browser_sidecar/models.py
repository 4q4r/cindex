"""Pydantic v2 request/response models for the browser sidecar API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class FetchRequest(BaseModel):
    """A single fetch request forwarded from the cindex worker.

    The sidecar opens a page on its persistent Chromium context, navigates to
    the URL (solving any JS challenge and setting cookies), then re-fetches the
    URL from inside the page so the raw server body is returned for every
    content type (HTML, XML, RSS, JSON).

    The ``json`` field is named ``json_body`` internally (to avoid shadowing
    ``BaseModel.json``) but exposed on the wire as ``json`` via its alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    url: HttpUrl
    method: str = Field(default="GET", pattern="^(GET|POST)$")
    params: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    data: dict[str, str] | None = None
    json_body: Any | None = Field(default=None, alias="json")
    accept: str | None = None
    timeout: float = Field(default=25.0, gt=0, le=120)


class ScreenshotRequest(BaseModel):
    """A screenshot request forwarded from the cindex worker.

    The sidecar opens a page on its persistent Chromium context, navigates to
    the URL (solving any JS challenge and setting cookies), then captures a
    full-page PNG. Used by the PERELMAN content fetcher for HTML articles
    without a PDF — the PNG is fed to the vision LLM as the ``page-shot``
    image. The PNG is returned inline (base64 in the ``FetchResponse`` body),
    never written to a shared filesystem (the sidecar has none with the worker).
    """

    url: HttpUrl
    timeout: float = Field(default=25.0, gt=0, le=120)


OCRLanguage = Literal[
    "ara",
    "deu",
    "eng",
    "spa",
    "fra",
    "ita",
    "jpn",
    "kor",
    "por",
    "rus",
    "chi_sim+chi_tra",
]

MAX_PDF_BASE64_CHARS = 45_000_000


class PDFTextRequest(BaseModel):
    """Base64 PDF bytes plus a validated Tesseract language hint."""

    body: str = Field(min_length=1, max_length=MAX_PDF_BASE64_CHARS)
    ocr_language: OCRLanguage = "eng"


class PDFTextResponse(BaseModel):
    """Normalized native/OCR text extracted from a PDF."""

    text: str


class FetchResponse(BaseModel):
    """The upstream response returned to the worker.

    The ``status`` field is the upstream server's HTTP status (not the
    sidecar's HTTP status); the worker maps ``status >= 400`` to a connector
    error. ``body`` carries the raw response payload, ``encoding`` declares
    how to interpret it:

    - ``"text"``: ``body`` is the browser-decoded text (the in-page ``fetch``
      already applied charset detection). The worker uses it directly for
      HTML/XML/JSON/RSS.
    - ``"base64"``: ``body`` is base64-encoded raw bytes (used for binary
      content types such as ``application/pdf`` so the worker can feed the
      bytes to a PDF parser without corruption).
    """

    status: int
    body: str
    content_type: str = ""
    encoding: str = "text"
