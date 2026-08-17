"""
Cross-lingual query translation for scholarly search.

Uses deep-translator (GoogleTranslator, no API key) with MyMemory fallback.
Translates short search queries (2-10 words) to target languages so each
source connector receives queries in its primary language, and Exa can search
across multiple languages.
"""

from __future__ import annotations

from functools import lru_cache

import structlog
from deep_translator import GoogleTranslator, MyMemoryTranslator

logger = structlog.get_logger(__name__)

SUPPORTED_LANGUAGES = ("en", "ru", "de", "fr", "es", "pt", "zh-CN", "ja", "ko")

SOURCE_LANGUAGES: dict[str, str] = {
    # API connectors
    "europe_pmc": "en",
    "openalex": "en",
    "crossref": "en",
    "pubmed": "en",
    "arxiv": "en",
    "doaj": "en",
    "pmc": "en",
    "core": "en",
    "biorxiv": "en",
    "medrxiv": "en",
    "dblp": "en",
    "hal": "fr",
    "zenodo": "en",
    "iacr": "en",
    "exa": "multi",
    # HTML connectors
    "cinii": "ja",
    "coaj": "zh-CN",
    "sciengine": "zh-CN",
    "sciopen": "zh-CN",
    "cyberleninka": "ru",
    "mathnet": "ru",
    "scielo": "es",
    "redalyc": "es",
    "korea_science": "ko",
    "persee": "fr",
    "open_edition": "fr",
    "revistas_csic": "es",
    "medknow": "en",
    "dergipark": "en",
    "hrcak": "en",
    "ajol": "en",
}

EXA_SEARCH_LANGUAGES = ("en", "ru", "de", "fr", "es", "zh-CN", "ja", "ko")

_CACHE_MAXSIZE = 512


def _detect_language(text: str) -> str:
    """
    Return the heuristic language code for a short query string.

    Args:
        text: A short query string to classify.

    Returns:
        An ISO 639-1 language code (``"en"``, ``"ru"``, ``"zh-CN"``,
        ``"ja"``, or ``"ko"``).

    """
    for c in text:
        if "Ѐ" <= c <= "ӿ":
            return "ru"
    for c in text:
        if "一" <= c <= "鿿":
            return "zh-CN"
        if "぀" <= c <= "ゟ" or "ㇰ" <= c <= "ヿ":
            return "ja"
        if "가" <= c <= "힯":
            return "ko"
    return "en"


@lru_cache(maxsize=_CACHE_MAXSIZE)
def translate_query(query: str, target_lang: str) -> str:
    """
    Translate a short scholarly query to *target_lang*.

    Uses GoogleTranslator (free, no key) with MyMemory fallback.
    Results are LRU-cached to avoid redundant API calls across search jobs.

    Args:
        query: The source query string (typically 2-10 words).
        target_lang: ISO 639-1 target language code.

    Returns:
        The translated query, or the original if both translators fail.

    """
    if not query or not query.strip():
        return query

    source_lang = _detect_language(query)
    if source_lang == target_lang:
        return query

    query_text = query.strip()[:500]

    try:
        result = GoogleTranslator(source="auto", target=target_lang).translate(
            text=query_text,
        )
        if result and result.strip():
            return result.strip()
    except (ValueError, RuntimeError, ConnectionError) as exc:
        logger.warning(
            "GoogleTranslator failed %s->%s: %s",
            source_lang,
            target_lang,
            exc,
        )

    try:
        result = MyMemoryTranslator(source="auto", target=target_lang).translate(
            text=query_text,
        )
        if result and result.strip():
            return result.strip()
    except (ValueError, RuntimeError, ConnectionError) as exc:
        logger.warning(
            "MyMemoryTranslator failed %s->%s: %s",
            source_lang,
            target_lang,
            exc,
        )

    logger.error("All translators failed for query=%s target=%s", query, target_lang)
    return query


def get_source_query_language(source_key: str) -> str:
    """
    Return the primary query language for a given source key.

    Args:
        source_key: The connector source key (e.g. ``"cinii"``, ``"hal"``).

    Returns:
        An ISO 639-1 language code, or ``"multi"`` for multi-language sources.

    """
    return SOURCE_LANGUAGES.get(source_key, "en")


def translate_query_for_source(query: str, source_key: str) -> str:
    """
    Translate *query* to the primary language of *source_key*.

    Args:
        query: The original user query.
        source_key: The connector source key.

    Returns:
        The translated query, or the original if the source is multi-language.

    """
    target = get_source_query_language(source_key)
    if target == "multi":
        return query
    return translate_query(query, target)


def expand_query_for_exa(query: str) -> dict[str, str]:
    """
    Translate the query into all Exa target languages.

    Args:
        query: The original user query.

    Returns:
        A dict mapping language codes to translated query strings.
        Used by ExaConnector to make one API call per language.

    """
    results: dict[str, str] = {}
    for lang in EXA_SEARCH_LANGUAGES:
        translated = translate_query(query, lang)
        if translated:
            results[lang] = translated
    return results


def expand_search_terms(query: str) -> list[str]:
    """
    Return deduplicated cross-lingual translations of *query*.

    Used by SearchService to expand DB search beyond the original language.

    Args:
        query: The original user query.

    Returns:
        A list of unique translated query strings (excluding the original).

    """
    seen: set[str] = {query.strip().casefold()}
    terms: list[str] = []
    for lang in ("en", "ru", "de", "fr", "es"):
        translated = translate_query(query, lang)
        normalized = translated.strip().casefold()
        if normalized and normalized not in seen:
            terms.append(translated.strip())
            seen.add(normalized)
    return terms
