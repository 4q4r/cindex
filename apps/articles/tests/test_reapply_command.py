"""Management command ``reapply_eligibility`` tests."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

from django.core.management import call_command

from apps.articles.models import Article, Journal, Source


def _make_article(source_key: str, *, abstract: str = "generic abstract") -> Article:
    """Persist an article for the given source and return it."""
    source, _ = Source.objects.get_or_create(
        key=source_key,
        defaults={"name": source_key, "base_url": "https://example.org"},
    )
    journal, _ = Journal.objects.get_or_create(name="Journal of Tests")
    doi = f"10.1000/cmd-{source_key}"
    return Article.objects.create(
        source=source,
        journal=journal,
        title=f"Study {source_key}",
        abstract=abstract,
        full_text=f"DOI {doi} body",
        doi=doi,
        url=f"https://example.org/{source_key}",
    )


def test_command_runs_inline(db) -> None:
    """The default inline path runs the backfill and prints a summary line."""
    _make_article("pubmed", abstract="medical study")
    _make_article("arxiv", abstract="physics preprint")
    out = StringIO()
    call_command("reapply_eligibility", stdout=out)
    text = out.getvalue()
    assert "Reapplied eligibility" in text
    assert "total=2" in text
    assert "peer_reviewed=1" in text
    assert "preprint=1" in text


def test_command_scoped_to_source(db) -> None:
    """``--source`` scopes the inline backfill to the named source."""
    _make_article("pubmed", abstract="medical study")
    _make_article("arxiv", abstract="physics preprint")
    out = StringIO()
    call_command("reapply_eligibility", "--source", "pubmed", stdout=out)
    assert "total=1" in out.getvalue()


def test_command_async_enqueues_task(db, monkeypatch) -> None:
    """``--async`` enqueues the celery task and prints the task id."""
    fake_result = MagicMock()
    fake_result.id = "task-abc-123"
    fake_delay = MagicMock(return_value=fake_result)
    from apps.articles.management.commands import reapply_eligibility as cmd_mod

    monkeypatch.setattr(cmd_mod.reapply_eligibility, "delay", fake_delay)

    out = StringIO()
    call_command("reapply_eligibility", "--async", stdout=out)
    text = out.getvalue()
    assert "Enqueued reapply_eligibility task id=task-abc-123" in text
    fake_delay.assert_called_once()
    call_kwargs = fake_delay.call_args.kwargs
    assert call_kwargs["source_keys"] is None
    assert call_kwargs["chunk_size"] == 500
