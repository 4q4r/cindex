"""Local scholarly file ingestion: filename metadata parsing and text extraction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote_plus

import pymupdf
from bs4 import BeautifulSoup
from django.utils import timezone

from apps.core.text import normalize_scholarly_text
from apps.ingestion.connectors import (
    BaseConnector,
    RawArticle,
)
from apps.ingestion.models import LocalImportFile
from apps.ingestion.services import IngestionService

if TYPE_CHECKING:
    from pathlib import Path

    from apps.articles.models import Article

SUPPORTED_IMPORT_SUFFIXES = {
    ".html",
    ".htm",
    ".json",
    ".jsonl",
    ".md",
    ".pdf",
    ".txt",
    ".xml",
}

SCANNED_LOCK_SECONDS = 5 * 60
_YEAR_DIGITS = 4
_YEAR_MIN = 1900
_YEAR_MAX = 2100


@dataclass(frozen=True)
class LocalImportMetadata:
    """Filename-derived metadata for a local scholarly import."""

    title: str
    authors: tuple[str, ...]
    doi: str
    journal: str
    year: int | None
    volume: str
    issue: str
    pages: str
    language: str
    source_key: str
    url: str
    abstract: str
    peer_review_evidence: str
    indexing_evidence: str
    preprint_evidence: str


def _split_authors(value: str) -> tuple[str, ...]:
    """Split a filename author segment into ordered author names."""
    normalized = unquote_plus(value).replace("_", " ").strip()
    if not normalized:
        return ()
    normalized = normalized.replace("|", ";").replace(",", ";")
    return tuple(part.strip() for part in normalized.split(";") if part.strip())


def _parse_year(value: str) -> int | None:
    """Parse a year from a filename segment."""
    normalized = unquote_plus(value).strip()
    if len(normalized) != _YEAR_DIGITS or not normalized.isdigit():
        return None
    year = int(normalized)
    if _YEAR_MIN <= year <= _YEAR_MAX:
        return year
    return None


def parse_local_import_metadata(
    path: Path,
    *,
    source_key: str | None = None,
) -> LocalImportMetadata:
    """Parse metadata from a local import filename."""
    stem = path.name
    if path.suffix.lower() in SUPPORTED_IMPORT_SUFFIXES:
        stem = path.with_suffix("").name
    segments = [segment for segment in stem.split("__") if segment]
    fields: dict[str, str] = {}
    title_fallback = ""
    for segment in segments:
        if "=" not in segment:
            if not title_fallback:
                title_fallback = unquote_plus(segment).replace("_", " ").strip()
            continue
        key, value = segment.split("=", 1)
        fields[key.strip().lower()] = unquote_plus(value).strip()

    title = fields.get("title", "").replace("_", " ").strip() or title_fallback
    authors = _split_authors(fields.get("authors", fields.get("author", "")))
    doi = fields.get("doi", "").strip().lower()
    journal = fields.get("journal", "").replace("_", " ").strip()
    year = _parse_year(fields.get("year", ""))
    volume = fields.get("volume", "").strip()
    issue = fields.get("issue", "").strip()
    pages = fields.get("pages", "").strip()
    language = fields.get("lang", fields.get("language", "")).strip() or "en"
    normalized_source = (
        source_key or fields.get("source", "")
    ).strip() or "local_import"
    url = fields.get("url", "").strip()
    abstract = fields.get("abstract", "").replace("_", " ").strip()
    peer_review_evidence = fields.get("peer_review", fields.get("refereed", "")).strip()
    indexing_evidence = fields.get("indexing", "").strip()
    preprint_evidence = fields.get("preprint", "").strip()
    if not title:
        title = path.stem.replace("_", " ").strip()
    if not journal:
        journal = normalized_source.replace("_", " ").strip() or normalized_source
    if not url:
        url = f"https://local-import.invalid/{quote(path.as_posix(), safe='')}"
    return LocalImportMetadata(
        title=title,
        authors=authors,
        doi=doi,
        journal=journal,
        year=year,
        volume=volume,
        issue=issue,
        pages=pages,
        language=language,
        source_key=normalized_source,
        url=url,
        abstract=abstract,
        peer_review_evidence=peer_review_evidence,
        indexing_evidence=indexing_evidence,
        preprint_evidence=preprint_evidence,
    )


def _extract_text_from_pdf(path: Path, *, ocr_language: str = "eng") -> str:
    """Extract text from a PDF file."""
    try:
        document = pymupdf.open(str(path))
    except (ValueError, RuntimeError, OSError):
        return ""
    pages: list[str] = []
    for page in document:
        try:
            text = page.get_text("text", sort=True).strip()
        except (ValueError, RuntimeError):
            text = ""
        if not text or "\ufffd" in text:
            try:
                textpage = page.get_textpage_ocr(language=ocr_language)
                text = page.get_text(textpage=textpage).strip()
            except (ValueError, RuntimeError, OSError):
                text = ""
        if text:
            pages.append(text)
    return normalize_scholarly_text("\n".join(pages))


def _extract_text_from_json(text: str) -> str:
    """Extract text from a JSON or JSONL local import."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return normalize_scholarly_text(text)
    if isinstance(payload, dict):
        parts = [
            str(payload.get("title", "")),
            str(payload.get("abstract", "")),
            str(payload.get("full_text", "")),
            str(payload.get("content", "")),
            str(payload.get("body", "")),
            str(payload.get("text", "")),
        ]
        return normalize_scholarly_text(" ".join(part for part in parts if part))
    if isinstance(payload, list):
        return normalize_scholarly_text(
            " ".join(
                str(item.get("text", item)) if isinstance(item, dict) else str(item)
                for item in payload
            ),
        )
    return normalize_scholarly_text(text)


def extract_local_import_text(path: Path, *, ocr_language: str = "eng") -> str:
    """Extract article text from a local import file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_text_from_pdf(path, ocr_language=ocr_language)

    raw_text = path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm", ".xml"}:
        soup = BaseConnector._sanitize_html_soup(  # noqa: SLF001
            BeautifulSoup(raw_text, "lxml"),
        )
        return BaseConnector._html_text(soup)  # noqa: SLF001
    if suffix in {".json", ".jsonl"}:
        return _extract_text_from_json(raw_text)
    return normalize_scholarly_text(raw_text)


def _headline_from_text(text: str) -> str:
    """Extract a fallback title from article text."""
    for line in text.splitlines():
        candidate = normalize_scholarly_text(line, max_length=300)
        if candidate:
            return candidate
    return ""


def build_raw_article_from_local_file(
    path: Path,
    *,
    source_key: str | None = None,
    digest: str | None = None,
) -> tuple[RawArticle, str]:
    """Build a raw article payload from a local import file."""
    metadata = parse_local_import_metadata(path, source_key=source_key)
    full_text = extract_local_import_text(path, ocr_language=metadata.language)
    title = metadata.title or _headline_from_text(full_text) or path.stem
    abstract = metadata.abstract or full_text[:1200]
    digest = digest or hashlib.sha256(path.read_bytes()).hexdigest()
    relative_url = quote(path.as_posix(), safe="")
    article_url = metadata.url or f"https://local-import.invalid/{relative_url}"
    raw_article = RawArticle(
        source_key=metadata.source_key,
        title=title,
        url=article_url,
        abstract=abstract,
        full_text=full_text,
        language=metadata.language,
        year=metadata.year,
        doi=metadata.doi,
        journal=metadata.journal,
        authors=metadata.authors,
        volume=metadata.volume,
        issue=metadata.issue,
        pages=metadata.pages,
        peer_review_evidence=metadata.peer_review_evidence,
        indexing_evidence=metadata.indexing_evidence,
        preprint_evidence=metadata.preprint_evidence,
    )
    return raw_article, digest


class LocalImportService:
    """Ingest local article files without restarting the stack."""

    @staticmethod
    def _path_key(drop_dir: Path, path: Path) -> str:
        """Return a stable relative key for a drop-folder file."""
        try:
            return path.resolve().relative_to(drop_dir.resolve()).as_posix()
        except (ValueError, RuntimeError):
            return path.name

    @classmethod
    def _should_skip(cls, path: Path) -> bool:
        """Return whether a file should be ignored."""
        if not path.is_file():
            return True
        return (
            path.name.startswith(".")
            or path.suffix.lower() not in SUPPORTED_IMPORT_SUFFIXES
        )

    @classmethod
    def ingest_file(cls, drop_dir: Path, path: Path) -> tuple[Article | None, bool]:
        """Ingest a single local file into the scholarly index."""
        if cls._should_skip(path):
            return None, False
        relative_path = cls._path_key(drop_dir, path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        record, _ = LocalImportFile.objects.get_or_create(path=relative_path)
        if (
            record.sha256 == digest
            and record.status == "completed"
            and record.article_id is not None
        ):
            return record.article, False

        try:
            raw_payload, _ = build_raw_article_from_local_file(
                path,
                digest=digest,
            )
            if not raw_payload.doi or not raw_payload.doi.startswith("10."):
                record.sha256 = digest
                record.status = "skipped"
                record.error = ""
                record.metadata = {
                    "source_key": raw_payload.source_key,
                    "title": raw_payload.title,
                    "doi": raw_payload.doi,
                    "skip_reason": "no_valid_doi",
                }
                record.save(update_fields=["sha256", "status", "error", "metadata"])
                return None, False
            article = IngestionService._save_article(raw_payload)  # noqa: SLF001
            record.sha256 = digest
            record.status = "completed"
            record.article = article
            record.error = ""
            record.metadata = {
                "source_key": raw_payload.source_key,
                "title": raw_payload.title,
                "doi": raw_payload.doi,
                "journal": raw_payload.journal,
                "year": raw_payload.year,
                "authors": list(raw_payload.authors),
                "full_text_length": len(raw_payload.full_text),
            }
            record.processed_at = timezone.now()
            record.save(
                update_fields=[
                    "sha256",
                    "status",
                    "article",
                    "error",
                    "metadata",
                    "processed_at",
                ],
            )
        except Exception as exc:
            record.sha256 = digest
            record.status = "failed"
            record.error = str(exc)
            record.metadata = {
                "source_key": raw_payload.source_key,
                "title": raw_payload.title,
                "doi": raw_payload.doi,
                "journal": raw_payload.journal,
                "year": raw_payload.year,
                "authors": list(raw_payload.authors),
            }
            record.save(
                update_fields=["sha256", "status", "error", "metadata", "article"],
            )
            raise
        else:
            return article, True

    @classmethod
    def scan_drop_dir(cls, drop_dir: Path) -> dict[str, int]:
        """Scan the local import folder and ingest changed files."""
        drop_dir.mkdir(parents=True, exist_ok=True)
        scanned = imported = skipped = failed = 0
        for path in sorted(drop_dir.rglob("*")):
            if cls._should_skip(path):
                continue
            scanned += 1
            try:
                _, was_imported = cls.ingest_file(drop_dir, path)
            except (ValueError, RuntimeError, ConnectionError):
                failed += 1
                continue
            if was_imported:
                imported += 1
            else:
                skipped += 1
        return {
            "scanned": scanned,
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
        }
