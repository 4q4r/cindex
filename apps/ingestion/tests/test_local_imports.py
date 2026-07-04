from __future__ import annotations

from pathlib import Path

from apps.ingestion import local_imports
from apps.ingestion.local_imports import (
    LocalImportService,
    extract_local_import_text,
    parse_local_import_metadata,
)
from apps.ingestion.models import LocalImportFile


def test_parse_local_import_metadata_extracts_fields() -> None:
    """Filename metadata must be parsed into structured fields."""

    path = Path(
        "title=AI in medicine__authors=Jane Doe;John Smith__doi=10.1234%2Fexample.1"
        "__year=2024__journal=Test Journal__source=pubmed.pdf"
    )

    metadata = parse_local_import_metadata(path)

    assert metadata.title == "AI in medicine"
    assert metadata.authors == ("Jane Doe", "John Smith")
    assert metadata.doi == "10.1234/example.1"
    assert metadata.year == 2024
    assert metadata.journal == "Test Journal"
    assert metadata.source_key == "pubmed"


def test_scan_drop_dir_ingests_and_skips_unchanged_file(
    monkeypatch: object, tmp_path: Path, db
) -> None:
    """Scan must ingest changed files and skip unchanged ones."""

    drop_dir = tmp_path / "local_imports"
    drop_dir.mkdir()
    file_path = drop_dir / (
        "title=Peer reviewed indexed article__authors=Jane Doe;John Smith"
        "__doi=10.9999%2Flocal.1__year=2024__journal=Local Journal.txt"
    )
    file_path.write_text(
        "This peer reviewed journal article is indexed in scopus and web of science. "
        "DOI 10.9999/local.1",
        encoding="utf-8",
    )

    first = LocalImportService.scan_drop_dir(drop_dir)
    record = LocalImportFile.objects.get(path=file_path.name)
    assert first == {"scanned": 1, "imported": 1, "skipped": 0, "failed": 0}
    assert record.status == "completed"
    assert record.article is not None
    assert record.article.is_eligible is True

    second = LocalImportService.scan_drop_dir(drop_dir)
    assert second == {"scanned": 1, "imported": 0, "skipped": 1, "failed": 0}


class _OcrPage:
    """Dummy local-import PDF page for OCR fallback tests."""

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
    """Dummy local-import PDF document for OCR fallback tests."""

    def __iter__(self):
        return iter([_OcrPage()])


def test_extract_local_import_text_uses_ocr_for_garbled_pdf(
    monkeypatch, tmp_path: Path
) -> None:
    """PDF local import extraction should use OCR when native text is garbled."""

    monkeypatch.setattr(
        local_imports.pymupdf, "open", lambda *args, **kwargs: _OcrDocument()
    )
    path = tmp_path / "article.pdf"
    path.write_bytes(b"%PDF-1.4 dummy")

    text = extract_local_import_text(path)

    assert "Hello PDF world for scholarly extraction" in text
    assert "���" not in text
