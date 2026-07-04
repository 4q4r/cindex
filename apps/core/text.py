from __future__ import annotations

import re
from html import unescape

HTML_TAG_RE = re.compile(r"<[^>]+>")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WHITESPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_scholarly_text(value: str, max_length: int | None = None) -> str:
    """Normalize scholarly text for indexing and display.

    Removes HTML tags and control characters, normalizes whitespace.
    Does NOT truncate or filter binary/PDF content — the reader handles that.
    """
    text = unescape(value or "").strip()
    if not text:
        return ""
    text = CONTROL_CHAR_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if max_length is not None and max_length >= 0:
        text = text[:max_length].rstrip()
    return text


def canonical_text_key(value: str) -> str:
    """Produce a stable key for title-based deduplication."""
    text = normalize_scholarly_text(value)
    if not text:
        return ""
    text = text.lower()
    text = NON_WORD_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_doi(value: str) -> str:
    """Normalize DOI-like identifiers for canonical deduplication."""
    return normalize_scholarly_text(value).lower().strip()
