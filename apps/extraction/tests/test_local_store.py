"""Unit tests for :mod:`apps.extraction.local_store` (real fs, real ORM).

Exercises the freeze-on-save round-trip with a tmp ``CINDEX_ARTICLES_DIR``:
``LocalArticleStore.save`` writes a YAML front-matter + multi-section body
(``## Аннотация`` / ``## Полный текст`` / ``## Формулы`` / ``## Графики и
рисунки`` / ``## Извлечённые цитаты``), and ``exists`` / ``to_raw`` /
``read_quotes`` parse it back. ``to_raw`` merges onto a ``fallback_raw`` so
fields not stored in the md (``language`` / ``volume`` / ``issue`` / ``pages``
/ ``*_evidence``) are preserved from the upstream feed. ``ArticleMarkdownService.
save`` stamps ``article.local_md_path`` (relative) and persists it via
``save(update_fields=["local_md_path"])`` — verified against the real Django
ORM with ordered ``ArticleAuthor`` rows (authors round-trip through the md
front-matter). No mocks: the hand-rolled front-matter parser is exercised end
to end against genuine on-disk files.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from apps.articles.models import Article, ArticleAuthor, Author, Journal, Source
from apps.extraction.local_store import ArticleMarkdownService, LocalArticleStore
from apps.ingestion.connectors.base import RawArticle


@dataclass
class _Journal:
    name: str = "Journal of Tests"


@dataclass
class _Source:
    key: str = "test"


@dataclass
class _StubArticle:
    """Minimal stand-in for ``Article`` (no ``article_authors`` → authors=[])."""

    doi: str = "10.1007/s10994-024-00100-x"
    title: str = "A Study of Quarks"
    url: str = "https://example.com/article"
    abstract: str = "We demonstrate a verbatim result."
    full_text: str = "Body text with a quotable sentence."
    publication_year: int | None = 2024
    journal: _Journal = field(default_factory=_Journal)
    source: _Source = field(default_factory=_Source)


def _quotes() -> list[dict]:
    return [
        {
            "text": "We demonstrate a verbatim result.",
            "location": "abstract",
            "relevance": 0.9,
            "rationale": "core claim",
        },
    ]


def _formulas() -> list[dict]:
    return [{"latex": "$$E=mc^2$$", "location": "section 1", "caption": ""}]


def _figures() -> list[dict]:
    return [
        {
            "markdown": "| x | y |\n|---|---|\n| 1 | 2 |",
            "location": "figure 1",
            "caption": "Plot of y vs x.",
            "kind": "graph",
        },
    ]


def _fallback_raw() -> RawArticle:
    """A feed-side ``RawArticle`` carrying fields the md does not store."""
    return RawArticle(
        source_key="test",
        title="Fallback Title",
        url="https://fallback.example/a",
        abstract="fallback abstract",
        full_text="fallback full text",
        language="en",
        year=1900,
        doi="10.1007/s10994-024-00100-x",
        journal="Fallback Journal",
        authors=("Fallback Author",),
        volume="V7",
        issue="I2",
        pages="1-9",
        peer_review_evidence="pr-evidence",
        indexing_evidence="idx-evidence",
        preprint_evidence="pp-evidence",
    )


@pytest.fixture
def articles_dir(tmp_path, monkeypatch):
    """Point ``CINDEX_ARTICLES_DIR`` at a tmp dir and return it."""
    monkeypatch.setenv("CINDEX_ARTICLES_DIR", str(tmp_path))
    return tmp_path


class TestPath:
    def test_doi_replaces_slashes_with_underscores(self, articles_dir) -> None:
        path = LocalArticleStore._path("10.1007/s10994")

        assert path == articles_dir / "10.1007_s10994.md"

    def test_non_doi_key_is_sanitized(self, articles_dir) -> None:
        path = LocalArticleStore._path("some weird/key!")

        assert path == articles_dir / "some_weird_key.md"

    def test_empty_key_falls_back_to_article_filename(self, articles_dir) -> None:
        path = LocalArticleStore._path("")

        assert path == articles_dir / "article.md"


class TestSaveAndExists:
    def test_save_writes_front_matter_body_and_quotes(self, articles_dir) -> None:
        relpath = LocalArticleStore.save(_StubArticle(), _quotes())

        path = articles_dir / "10.1007_s10994-024-00100-x.md"
        assert path.is_file()
        assert relpath == "10.1007_s10994-024-00100-x.md"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "title: A Study of Quarks" in text
        assert "year: 2024" in text
        assert "doi: 10.1007/s10994-024-00100-x" in text
        assert "is_preprint: false" in text
        assert "## Аннотация" in text
        assert "## Полный текст" in text
        assert "## Извлечённые цитаты" in text
        assert "We demonstrate a verbatim result." in text
        assert "## TLDR" not in text

    def test_save_writes_tldr_section_when_present(self, articles_dir) -> None:
        relpath = LocalArticleStore.save(
            _StubArticle(),
            _quotes(),
            tldr="Краткое резюме статьи.",
        )

        text = (articles_dir / relpath).read_text(encoding="utf-8")
        assert text.index("## TLDR") < text.index("## Аннотация")
        assert "Краткое резюме статьи." in text

    def test_save_creates_missing_directory(self, tmp_path, monkeypatch) -> None:
        nested = tmp_path / "nested" / "articles"
        monkeypatch.setenv("CINDEX_ARTICLES_DIR", str(nested))

        LocalArticleStore.save(_StubArticle(doi="10.1/abc"), _quotes())

        assert (nested / "10.1_abc.md").is_file()

    def test_exists_true_after_save_false_otherwise(self, articles_dir) -> None:
        assert LocalArticleStore.exists("10.1007/s10994-024-00100-x") is False

        LocalArticleStore.save(_StubArticle(), _quotes())

        assert LocalArticleStore.exists("10.1007/s10994-024-00100-x") is True
        assert LocalArticleStore.exists("") is False


class TestToRaw:
    def test_restores_md_fields_and_preserves_fallback(self, articles_dir) -> None:
        LocalArticleStore.save(
            _StubArticle(),
            _quotes(),
            formulas=_formulas(),
            figures=_figures(),
        )

        raw = LocalArticleStore.to_raw(
            "10.1007/s10994-024-00100-x",
            fallback_raw=_fallback_raw(),
        )

        assert raw is not None
        # Text + metadata come from the md.
        assert raw.title == "A Study of Quarks"
        assert raw.abstract == "We demonstrate a verbatim result."
        assert raw.full_text == "Body text with a quotable sentence."
        assert raw.doi == "10.1007/s10994-024-00100-x"
        assert raw.url == "https://example.com/article"
        assert raw.journal == "Journal of Tests"
        assert raw.source_key == "test"
        assert raw.year == 2024
        # Fields not stored in the md are preserved from fallback_raw.
        assert raw.language == "en"
        assert raw.volume == "V7"
        assert raw.issue == "I2"
        assert raw.pages == "1-9"
        assert raw.peer_review_evidence == "pr-evidence"
        assert raw.indexing_evidence == "idx-evidence"
        assert raw.preprint_evidence == "pp-evidence"
        # No authors in the stub md → fallback authors preserved.
        assert raw.authors == ("Fallback Author",)

    def test_returns_none_when_file_missing(self, articles_dir) -> None:
        raw = LocalArticleStore.to_raw("10.0/missing", fallback_raw=_fallback_raw())

        assert raw is None

    def test_returns_none_when_front_matter_malformed(self, articles_dir) -> None:
        path = LocalArticleStore._path("10.1/bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntitle: x\nbody with no closing delimiter",
            encoding="utf-8",
        )

        raw = LocalArticleStore.to_raw("10.1/bad", fallback_raw=_fallback_raw())

        assert raw is None


class TestReadQuotes:
    def test_parses_quotes_section(self, articles_dir) -> None:
        LocalArticleStore.save(_StubArticle(), _quotes())

        quotes = LocalArticleStore.read_quotes("10.1007/s10994-024-00100-x")

        assert quotes is not None
        assert len(quotes) == 1
        q = quotes[0]
        assert q["text"] == "We demonstrate a verbatim result."
        assert q["location"] == "abstract"
        assert q["relevance"] == 0.9
        assert q["rationale"] == "core claim"

    def test_none_when_no_file(self, articles_dir) -> None:
        assert LocalArticleStore.read_quotes("10.0/missing") is None

    def test_empty_list_when_no_quotes_section(self, articles_dir) -> None:
        # No quotes → the section is omitted from the md.
        LocalArticleStore.save(_StubArticle(doi="10.1/noq"), [])

        assert LocalArticleStore.read_quotes("10.1/noq") == []


class TestFormulasAndFiguresRendering:
    def test_body_contains_formulas_and_figures_sections(self, articles_dir) -> None:
        LocalArticleStore.save(
            _StubArticle(),
            _quotes(),
            formulas=_formulas(),
            figures=_figures(),
        )

        text = (articles_dir / "10.1007_s10994-024-00100-x.md").read_text(
            encoding="utf-8",
        )
        assert "## Формулы" in text
        assert "- $$E=mc^2$$" in text
        assert "— location: section 1" in text
        assert "## Графики и рисунки" in text
        assert "### graph" in text
        assert "Plot of y vs x." in text


class TestArticleMarkdownService:
    def test_save_stamps_relative_local_md_path_and_persists(
        self,
        db,
        articles_dir,
    ) -> None:
        source = Source.objects.create(
            key="test",
            name="Test",
            base_url="https://example.org",
        )
        journal = Journal.objects.create(name="Journal of Tests")
        article = Article.objects.create(
            source=source,
            journal=journal,
            title="A Study of Quarks",
            abstract="We demonstrate a verbatim result.",
            full_text="Body text with a quotable sentence.",
            doi="10.1000/local-store-test",
            url="https://example.org/a1",
            publication_year=2024,
        )
        alice = Author.objects.create(full_name="Alice Quark")
        bob = Author.objects.create(full_name="Bob Boson")
        ArticleAuthor.objects.create(article=article, author=alice, order=1)
        ArticleAuthor.objects.create(article=article, author=bob, order=2)

        ArticleMarkdownService.save(article, _quotes(), _formulas(), _figures())

        article.refresh_from_db()
        assert article.local_md_path == "10.1000_local-store-test.md"
        assert (articles_dir / "10.1000_local-store-test.md").is_file()

        # Authors round-trip through the md front-matter via to_raw.
        raw = LocalArticleStore.to_raw(article.doi, fallback_raw=_fallback_raw())
        assert raw is not None
        assert raw.authors == ("Alice Quark", "Bob Boson")
