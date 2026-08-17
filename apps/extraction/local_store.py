"""
Local markdown store for PERELMAN-processed articles + freeze-on-save.

A published article that has been processed by the PERELMAN extractor is frozen
to a ``.md`` file under :attr:`LLMConfig.articles_dir`
(``CINDEX_ARTICLES_DIR``). The file carries the article's text plus the
extracted formulas (LaTeX) and figures (markdown) — «конвертировал в md от и до»
— so the agent's full output is durably preserved on disk.

Freeze semantics: :class:`ArticleMarkdownService.save` writes the md and stamps
``article.local_md_path`` (the path relative to ``CINDEX_ARTICLES_DIR``). A
non-empty ``local_md_path`` marks the article as frozen — on refresh the
ingestion pipeline reads the local md first (:meth:`LocalArticleStore.to_raw`)
and skips re-fetching from the network. Only published articles are frozen
(preprints are volatile and stay fully refreshable).

No ``pyyaml`` dependency: the front-matter is a minimal, hand-rolled
``key: value`` / ``- item`` parser (the only shapes this module writes).
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from .config import load_config

if TYPE_CHECKING:
    from apps.articles.models import Article
    from apps.ingestion.connectors.base import RawArticle

logger = structlog.get_logger(__name__)

# Sections written by :meth:`LocalArticleStore.save` (and read back by
# ``to_raw`` / ``read_quotes`). Order matters for human readability, not for
# parsing — the parser splits on whichever header is present.
_SECTION_TLDR = "## TLDR"
_SECTION_ABSTRACT = "## Аннотация"
_SECTION_FULL_TEXT = "## Полный текст"
_SECTION_FORMULAS = "## Формулы"
_SECTION_FIGURES = "## Графики и рисунки"
_SECTION_QUOTES = "## Извлечённые цитаты"

# Characters allowed in a sanitized filename component (no path separators).
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    """Return ``name`` reduced to ``[A-Za-z0-9._-]`` (empty → ``article``)."""
    safe = _SAFE_FILENAME_RE.sub("_", name).strip("._-")
    return safe or "article"


class LocalArticleStore:
    """
    Filesystem-backed markdown store for PERELMAN-processed articles.

    All lookups key off the article's DOI (the ``Article.doi`` field is unique
    and always present, though it may not start with ``10.``). A DOI-shaped key
    (``10.``-prefixed) maps to ``{doi_with_slashes_as_underscores}.md``; any
    other key is sanitized to a filesystem-safe filename.
    """

    @staticmethod
    def _path(doi_or_key: str) -> Path:
        """
        Return the absolute ``.md`` path for ``doi_or_key``.

        A ``10.``-prefixed key is treated as a DOI: ``/`` → ``_`` (preserving
        the DOI's readable shape). Any other key is sanitized to
        ``[A-Za-z0-9._-]``. The path lives under ``cfg.articles_dir``.
        """
        cfg = load_config()
        key = (doi_or_key or "").strip()
        if key.startswith("10."):
            safe = key.replace("/", "_")
        else:
            safe = _sanitize_filename(key)
        return Path(cfg.articles_dir) / f"{safe}.md"

    @classmethod
    def exists(cls, doi: str) -> bool:
        """Return ``True`` when a frozen md file exists for ``doi``."""
        return bool(doi) and cls._path(doi).is_file()

    @classmethod
    def to_raw(cls, doi: str, fallback_raw: RawArticle) -> RawArticle | None:
        """
        Build a :class:`RawArticle` for a frozen article from its local md.

        Merges the md content onto ``fallback_raw``: textual fields
        (``abstract`` / ``full_text``) and front-matter metadata
        (``title`` / ``authors`` / ``year`` / ``journal`` / ``doi`` / ``url`` /
        ``source_key``) come from the md; fields not stored in the md
        (``language`` / ``volume`` / ``issue`` / ``pages`` / ``*_evidence``)
        are preserved from ``fallback_raw`` so nothing the upstream feed
        carried is lost. Returns ``None`` on a missing file or parse failure so
        the caller can fall back to network enrichment.
        """
        if not doi:
            return None
        path = cls._path(doi)
        if not path.is_file():
            return None
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "local_store: md read failed",
                path=str(path),
                error=str(exc),
            )
            return None
        parsed = _parse_md(raw_text)
        if parsed is None:
            logger.warning("local_store: md parse failed", path=str(path))
            return None
        front, body = parsed
        abstract = _section_body(body, _SECTION_ABSTRACT) or fallback_raw.abstract
        full_text = _section_body(body, _SECTION_FULL_TEXT) or fallback_raw.full_text
        year_raw = front.get("year")
        try:
            year: int | None = int(year_raw) if year_raw not in (None, "") else None
        except (TypeError, ValueError):
            year = fallback_raw.year
        authors_raw = front.get("authors") or []
        authors = (
            tuple(authors_raw)
            if isinstance(authors_raw, list)
            else fallback_raw.authors
        )
        return replace(
            fallback_raw,
            title=front.get("title") or fallback_raw.title,
            abstract=abstract,
            full_text=full_text,
            doi=front.get("doi") or fallback_raw.doi,
            url=front.get("url") or fallback_raw.url,
            journal=front.get("journal") or fallback_raw.journal,
            source_key=front.get("source_key") or fallback_raw.source_key,
            year=year if year is not None else fallback_raw.year,
            authors=authors or fallback_raw.authors,
        )

    @classmethod
    def read_quotes(cls, doi: str) -> list[dict] | None:
        """
        Return the parsed ``## Извлечённые цитаты`` section, or ``None``.

        Each quote is restored as a ``{text, location, relevance, rationale}``
        dict. Returns ``None`` when no frozen md exists (cache-warm callers
        treat that as a miss).
        """
        if not doi:
            return None
        path = cls._path(doi)
        if not path.is_file():
            return None
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "local_store: md read failed",
                path=str(path),
                error=str(exc),
            )
            return None
        parsed = _parse_md(raw_text)
        if parsed is None:
            return None
        _front, body = parsed
        quotes_text = _section_body(body, _SECTION_QUOTES)
        if quotes_text is None:
            return []
        return _parse_quotes_section(quotes_text)

    @classmethod
    def save(
        cls,
        article: Article,
        quotes: list[dict],
        formulas: list[dict] = (),
        figures: list[dict] = (),
        *,
        tldr: str = "",
    ) -> str:
        """
        Write the article's frozen md and return its relative path.

        The md has a YAML-style front-matter (title / authors / year / journal
        / doi / url / source_key / ``is_preprint: false``) and a body with
        ``## TLDR`` (when present), ``## Аннотация``, ``## Полный текст``,
        ``## Формулы``, ``## Графики и рисунки``, ``## Извлечённые цитаты``.
        The articles dir is created if missing. The returned path is relative
        to ``CINDEX_ARTICLES_DIR``.
        """
        cfg = load_config()
        path = cls._path(article.doi)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _render_md(article, quotes, list(formulas), list(figures), tldr=tldr)
        path.write_text(content, encoding="utf-8")
        return (
            path.relative_to(Path(cfg.articles_dir).resolve()).as_posix()
            if Path(
                cfg.articles_dir,
            ).is_absolute()
            else path.name
        )


class ArticleMarkdownService:
    """
    Façade that freezes an article via :class:`LocalArticleStore`.

    Writes the md and stamps ``article.local_md_path`` (relative to
    ``CINDEX_ARTICLES_DIR``), persisting only that one field. Only published
    articles reach this service (preprints are never frozen).
    """

    @classmethod
    def save(
        cls,
        article: Article,
        quotes: list[dict],
        formulas: list[dict] = (),
        figures: list[dict] = (),
        *,
        tldr: str = "",
    ) -> Article:
        """Freeze ``article`` to local md and stamp ``local_md_path``."""
        relpath = LocalArticleStore.save(
            article,
            quotes,
            formulas,
            figures,
            tldr=tldr,
        )
        article.local_md_path = relpath
        article.save(update_fields=["local_md_path"])
        return article


# ---------------------------------------------------------------------------
# Front-matter + body rendering / parsing (no pyyaml)
# ---------------------------------------------------------------------------


def _render_md(
    article: Article,
    quotes: list[dict],
    formulas: list[dict],
    figures: list[dict],
    *,
    tldr: str = "",
) -> str:
    """Render the full markdown document for ``article``."""
    front = _render_front_matter(article)
    sections = [
        _render_tldr_section(tldr),
        f"{_SECTION_ABSTRACT}\n\n{(article.abstract or '').strip()}",
        f"{_SECTION_FULL_TEXT}\n\n{(article.full_text or '').strip()}",
        _render_formulas_section(formulas),
        _render_figures_section(figures),
        _render_quotes_section(quotes),
    ]
    return (
        front + "\n\n".join(s.rstrip() for s in sections if s.rstrip()).rstrip() + "\n"
    )


def _render_tldr_section(tldr: str) -> str:
    """
    Render the ``## TLDR`` section (empty string when there is no tldr).

    Whitespace (including newlines) is collapsed so the free-text summary
    stays on one line and can never smuggle a ``## `` section header into
    the md, which would confuse the ``_section_body`` parsers.
    """
    collapsed = re.sub(r"\s+", " ", tldr or "").strip()
    if not collapsed:
        return ""
    return f"{_SECTION_TLDR}\n\n{collapsed}"


def _render_formulas_section(formulas: list[dict]) -> str:
    """Render the ``## Формулы`` section (one ``- $$latex$$`` item per formula)."""
    lines = [f"{_SECTION_FORMULAS}\n"]
    for f in formulas:
        latex = _safe_str(f.get("latex"))
        if not latex:
            continue
        location = _safe_str(f.get("location"))
        caption = _safe_str(f.get("caption"))
        entry = f"- {latex}"
        if location:
            entry += f"  \n  — location: {location}"
        if caption:
            entry += f"  \n  — caption: {caption}"
        lines.append(entry)
    return "\n".join(lines)


def _render_figures_section(figures: list[dict]) -> str:
    """Render the ``## Графики и рисунки`` section (markdown-converted figures)."""
    lines = [f"{_SECTION_FIGURES}\n"]
    for fig in figures:
        markdown = _safe_str(fig.get("markdown"))
        if not markdown:
            continue
        location = _safe_str(fig.get("location"))
        caption = _safe_str(fig.get("caption"))
        kind = _safe_str(fig.get("kind")) or "figure"
        lines.append(f"### {kind}")
        if location:
            lines.append(f"*location: {location}*")
        lines.append(markdown)
        if caption:
            lines.append(f"\n*{caption}*")
        lines.append("")
    return "\n".join(lines)


def _render_quotes_section(quotes: list[dict]) -> str:
    """Render the ``## Извлечённые цитаты`` section (``- text: ...`` items)."""
    lines = [f"{_SECTION_QUOTES}\n"]
    for q in quotes:
        text = _safe_str(q.get("text"))
        if not text:
            continue
        location = _safe_str(q.get("location"))
        relevance = q.get("relevance")
        rationale = _safe_str(q.get("rationale"))
        lines.append(f"- text: {text}")
        if location:
            lines.append(f"  location: {location}")
        if relevance is not None:
            lines.append(f"  relevance: {relevance}")
        if rationale:
            lines.append(f"  rationale: {rationale}")
        lines.append("")
    return "\n".join(lines)


def _render_front_matter(article: Article) -> str:
    """Render the YAML-style front-matter block for ``article``."""
    title = _safe_str(getattr(article, "title", ""))
    doi = _safe_str(getattr(article, "doi", ""))
    url = _safe_str(getattr(article, "url", ""))
    year = getattr(article, "publication_year", None)
    journal_obj = getattr(article, "journal", None)
    journal = _safe_str(getattr(journal_obj, "name", "")) if journal_obj else ""
    source_obj = getattr(article, "source", None)
    source_key = _safe_str(getattr(source_obj, "key", "")) if source_obj else ""
    authors = _article_authors(article)
    lines = ["---"]
    if title:
        lines.append(f"title: {title}")
    if authors:
        lines.append("authors:")
        lines.extend(f"  - {name}" for name in authors)
    if year is not None:
        lines.append(f"year: {year}")
    if journal:
        lines.append(f"journal: {journal}")
    if doi:
        lines.append(f"doi: {doi}")
    if url:
        lines.append(f"url: {url}")
    if source_key:
        lines.append(f"source_key: {source_key}")
    lines.append("is_preprint: false")
    lines.append("---\n")
    return "\n".join(lines) + "\n"


def _article_authors(article: Article) -> list[str]:
    """
    Return ordered author full names from the ``article_authors`` relation.

    Defensive against non-Django stubs (returns ``[]`` when the relation is
    absent) so the renderer can be unit-tested without an ORM round-trip.
    """
    rel = getattr(article, "article_authors", None)
    if rel is None:
        return []
    try:
        ordered = rel.all().order_by("order")
    except AttributeError:
        # A plain list/callable stub without .order_by — use it as-is.
        ordered = rel.all() if callable(getattr(rel, "all", None)) else rel
    out: list[str] = []
    for aa in ordered:
        author = getattr(aa, "author", None)
        name = _safe_str(getattr(author, "full_name", None))
        if name:
            out.append(name)
    return out


def _parse_md(text: str) -> tuple[dict, str] | None:
    """
    Split ``text`` into ``(front_matter_dict, body)``.

    Returns ``None`` when the front-matter delimiters are malformed. The body
    is everything after the closing ``---``. Front-matter values are typed:
    ``year`` → int, ``is_preprint`` → bool, ``authors`` → list[str], others →
    str.
    """
    if not text.startswith("---"):
        # No front-matter: treat the whole text as body (still parseable).
        return {}, text
    # Find the closing delimiter on its own line.
    close_match = re.search(r"\n---\s*\n", text)
    if close_match is None:
        return None
    front_text = text[3 : close_match.start()]  # skip leading "---\n"
    body = text[close_match.end() :]
    return _parse_front_matter(front_text), body


def _parse_front_matter(front_text: str) -> dict:
    """Parse a minimal ``key: value`` / ``- item`` block into a dict."""
    out: dict = {}
    current_list: str | None = None
    for raw_line in front_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            current_list = None
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current_list is not None:
                out.setdefault(current_list, []).append(stripped[2:].strip())
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "":
                # A list header (e.g. "authors:") — following "- " lines fill it.
                current_list = key
                out.setdefault(key, [])
            else:
                current_list = None
                out[key] = _coerce_front_value(key, value)
    return out


def _coerce_front_value(key: str, value: str) -> object:
    """Coerce a front-matter scalar to its typed Python value."""
    if key == "year":
        try:
            return int(value)
        except ValueError:
            return value
    if key == "is_preprint":
        return value.lower() in {"true", "1", "yes"}
    return value


def _section_body(body: str, header: str) -> str | None:
    """
    Return the text under ``header`` up to the next ``## `` section.

    Returns ``None`` when the header is absent. Headers are matched at a line
    start (every ``## `` section is written on its own line) so a free-text
    section body can never hijack another section via a literal substring.
    Leading/trailing blank lines are stripped.
    """
    idx = body.find("\n" + header)
    if idx == -1:
        if not body.startswith(header):
            return None
        idx = 0
    start = idx + len(header) + 1 if idx else len(header)
    # Next top-level "## " header after this section.
    next_idx = body.find("\n## ", start)
    section = body[start:next_idx] if next_idx != -1 else body[start:]
    return section.strip("\n").strip()


def _parse_quotes_section(text: str) -> list[dict]:
    """
    Parse the ``## Извлечённые цитаты`` body into a list of quote dicts.

    Each quote is a ``- text: ...`` item followed by indented
    ``location:`` / ``relevance:`` / ``rationale:`` sub-fields.
    """
    quotes: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.startswith("- "):
            if current is not None:
                quotes.append(current)
            current = _new_quote(raw_line[2:].strip())
        elif current is not None and raw_line.startswith("  "):
            _apply_quote_field(current, raw_line.strip())
    if current is not None:
        quotes.append(current)
    return quotes


def _new_quote(raw_text: str) -> dict:
    """Start a new quote dict from a ``- text: <value>`` (or bare) item line."""
    text = raw_text.strip()
    if text.startswith("text:"):
        text = text[5:].strip()
    return {"text": text, "location": "", "relevance": 0.0, "rationale": ""}


def _apply_quote_field(quote: dict, line: str) -> None:
    """Apply one indented ``key: value`` sub-field to ``quote`` in place."""
    key, _, value = line.partition(":")
    key = key.strip()
    value = value.strip()
    if key == "location":
        quote["location"] = value
    elif key == "rationale":
        quote["rationale"] = value
    elif key == "relevance":
        try:
            quote["relevance"] = float(value)
        except ValueError:
            quote["relevance"] = 0.0


def _safe_str(value: object) -> str:
    """Return ``str(value).strip()`` or ``""`` for ``None``."""
    if value is None:
        return ""
    return str(value).strip()
