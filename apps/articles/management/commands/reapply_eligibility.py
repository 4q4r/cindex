"""Django management command to reapply article eligibility in place.

Re-runs ``ArticleEligibilityService.apply`` over the persisted corpus to
backfill articles ingested before the connectors emitted peer-review /
preprint / indexing tier evidence. The classifier reclassifies them from
``article.source.key`` (source-reputation default) and the stored
title / abstract / full_text -- no network fetch.

Runs inline by default (no broker needed). Pass ``--async`` to enqueue the
``apps.articles.tasks.reapply_eligibility`` celery task instead.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.articles.tasks import reapply_eligibility


class Command(BaseCommand):
    """Reapply eligibility decisions to existing articles."""

    help = (
        "Re-run the tiered eligibility classifier over existing articles "
        "without re-fetching sources."
    )

    def add_arguments(self, parser: Any) -> None:  # noqa: ANN401  # Django ArgumentParser
        """Add the source-scope, chunk-size, and async options."""
        parser.add_argument(
            "--source",
            action="append",
            dest="source_keys",
            default=None,
            help=(
                "Restrict to these connector source keys (repeatable, e.g. "
                "--source pubmed --source crossref). Default: all sources."
            ),
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=500,
            help="Queryset iterator chunk size (default: 500).",
        )
        parser.add_argument(
            "--async",
            action="store_true",
            dest="async_",
            default=False,
            help="Enqueue a celery task instead of running inline.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401, ARG002  # BaseCommand
        """Run the eligibility backfill inline or enqueue it as a celery task."""
        source_keys: list[str] | None = options["source_keys"]
        chunk_size: int = options["chunk_size"]
        if options["async_"]:
            result = reapply_eligibility.delay(
                source_keys=source_keys,
                chunk_size=chunk_size,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Enqueued reapply_eligibility task id={result.id}",
                ),
            )
            return

        eager = reapply_eligibility.apply(
            kwargs={"source_keys": source_keys, "chunk_size": chunk_size},
        )
        payload: dict[str, int] = eager.get() if hasattr(eager, "get") else eager
        self.stdout.write(
            self.style.SUCCESS(
                "Reapplied eligibility: "
                f"total={payload['total']} "
                f"peer_reviewed={payload['peer_reviewed']} "
                f"indexed={payload['indexed']} "
                f"preprint={payload['preprint']} "
                f"eligible={payload['eligible']} "
                f"failed={payload['failed']}",
            ),
        )
