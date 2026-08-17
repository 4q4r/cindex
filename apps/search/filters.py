"""
Server-side search filter and sort parameters.

These parameters are applied inside :class:`apps.search.services.SearchService`
*before* the top-K truncation so that strict filters do not discard eligible
articles that ranked just outside the top-K window. They are persisted on
:class:`apps.search.models.SearchJob` so the asynchronous job path applies the
same filtering/sorting the requesting client asked for.
"""

from __future__ import annotations

from dataclasses import dataclass

SORT_RELEVANCE = "relevance"
SORT_NEWEST = "newest"
SORT_METADATA = "metadata"
SORT_CHOICES: tuple[str, ...] = (SORT_RELEVANCE, SORT_NEWEST, SORT_METADATA)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """
    Server-side filter and sort parameters for article search.

    Attributes:
        peer_reviewed_only: Keep only articles flagged as peer-reviewed/refereed.
        indexed_only: Keep only articles flagged as indexed in a reputable DB.
        exclude_preprints: Keep only articles flagged as not a preprint or
            author manuscript.
        exclude_retracted: Keep only articles not flagged as retracted.
        year_from: Inclusive lower bound on ``publication_year`` (None = unbounded).
        year_to: Inclusive upper bound on ``publication_year`` (None = unbounded).
        sort_by: Ranking key -- one of :data:`SORT_CHOICES`.

    """

    peer_reviewed_only: bool = False
    indexed_only: bool = False
    exclude_preprints: bool = False
    exclude_retracted: bool = False
    year_from: int | None = None
    year_to: int | None = None
    sort_by: str = SORT_RELEVANCE

    def normalized_sort(self) -> str:
        """Return ``sort_by`` clamped to a valid choice."""
        return self.sort_by if self.sort_by in SORT_CHOICES else SORT_RELEVANCE

    def is_default(self) -> bool:
        """Return True when no filter/sort overrides are active."""
        return (
            not self.peer_reviewed_only
            and not self.indexed_only
            and not self.exclude_preprints
            and not self.exclude_retracted
            and self.year_from is None
            and self.year_to is None
            and self.normalized_sort() == SORT_RELEVANCE
        )

    def signature(self) -> str:
        """
        Return a stable string used to deduplicate concurrent search jobs.

        Two jobs with the same query/expression but different filters must not
        attach to each other, so the filter signature participates in the
        active-job lookup and creation-lock key material.
        """
        return "|".join(
            (
                str(int(self.peer_reviewed_only)),
                str(int(self.indexed_only)),
                str(int(self.exclude_preprints)),
                str(int(self.exclude_retracted)),
                "" if self.year_from is None else str(self.year_from),
                "" if self.year_to is None else str(self.year_to),
                self.normalized_sort(),
            ),
        )


DEFAULT_FILTERS = SearchFilters()
