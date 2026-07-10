"""Tests for connector quick-win fixes: author/volume/journal/abstract extraction."""

from typing import ClassVar

import pytest

from apps.ingestion.connectors import (
    AJOLConnector,
    BaseConnector,
    CrossrefConnector,
    CyberLeninkaConnector,
    EuropePMCConnector,
    HALConnector,
    MathNetConnector,
    OpenAlexConnector,
    PMCConnector,
    SciELOConnector,
)


class TestEuropePCMAuthors:
    """EuropePMCConnector should extract authors from authorList and authorString."""

    def test_author_list_with_full_name(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "Test article",
                        "abstractText": "Abstract",
                        "journalTitle": "Test J",
                        "doi": "10.1234/test",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.com/article"}],
                        },
                        "authorList": {
                            "author": [
                                {"fullName": "Alice Smith"},
                                {"firstName": "Bob", "lastName": "Jones"},
                            ],
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Alice Smith", "Bob Jones")

    def test_author_string_fallback(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "Test article",
                        "abstractText": "Abstract",
                        "journalTitle": "Test J",
                        "doi": "10.1234/test",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.com/article"}],
                        },
                        "authorString": "Smith A, Jones B, Lee C",
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Smith A", "Jones B", "Lee C")

    def test_no_authors(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "Test article",
                        "abstractText": "Abstract",
                        "journalTitle": "Test J",
                        "doi": "10.1234/test",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.com/article"}],
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ()


class TestPCMAuthors:
    """PMCConnector should extract authors same as EuropePMC."""

    def test_author_list_extraction(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "PMC article",
                        "abstractText": "Abstract",
                        "journalTitle": "PMC J",
                        "doi": "10.1234/pmc",
                        "pubYear": "2024",
                        "pmcid": "PMC1234567",
                        "authorList": {
                            "author": [
                                {"fullName": "Carol White"},
                                {"firstName": "Dave", "lastName": "Brown"},
                            ],
                        },
                    },
                ],
            },
        }
        conn = PMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Carol White", "Dave Brown")


class TestEuropePMCJournal:
    """EuropePMCConnector should read journal from journalInfo.journal.title.

    EuropePMC's ``resultType=core`` response has no top-level ``journalTitle``
    field; the journal title lives at ``journalInfo.journal.title``. Reading
    the non-existent top-level field always fell back to ``"Europe PMC"``,
    masking the real venue for every record.
    """

    def test_journal_from_journal_info(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "EPMC article",
                        "abstractText": "Abstract",
                        "doi": "10.1234/epmc",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.org/a"}],
                        },
                        "journalInfo": {
                            "journal": {
                                "title": "Genetics in medicine",
                            },
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Genetics in medicine"

    def test_falls_back_when_journal_info_missing(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "EPMC no journal",
                        "abstractText": "Abstract",
                        "doi": "10.1234/epmc2",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.org/b"}],
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Europe PMC"

    def test_ignores_top_level_journal_title(self) -> None:
        """A stray top-level ``journalTitle`` must not be trusted.

        The real EuropePMC API never emits this field; if a payload carries
        it (e.g. a stale fixture), the connector must still read from
        ``journalInfo.journal.title`` and not regress to the old behaviour.
        """
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "EPMC stray field",
                        "abstractText": "Abstract",
                        "doi": "10.1234/epmc3",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.org/c"}],
                        },
                        "journalTitle": "Stale Top-Level Value",
                        "journalInfo": {
                            "journal": {"title": "The Lancet"},
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "The Lancet"


class TestEuropePMCAbstract:
    """EuropePMCConnector must drop records with a null/empty abstract.

    EuropePMC returns ``abstractText: null`` (a present-but-null key, not a
    missing key) for editorials, letters and comments. ``rec.get("abstractText",
    "")`` returns None for that shape, which both injects "None" into full_text
    and surfaces an empty-abstract record as garbage. Such editorials also
    broad-match many medical queries (the same Lancet Global Health editorial
    surfaced for both 'diabetes' and 'cancer' in the live audit).
    """

    def _rec(self, title: str, abstract: object, doi: str) -> dict:
        return {
            "title": title,
            "abstractText": abstract,
            "doi": doi,
            "pubYear": "2024",
            "fullTextUrlList": {"fullTextUrl": [{"url": "https://example.org/x"}]},
        }

    def test_null_abstract_is_dropped(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    self._rec("Editorial", None, "10.1/ed"),
                    self._rec("Real Article", "A real abstract.", "10.1/a"),
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].title == "Real Article"
        assert items[0].abstract == "A real abstract."

    def test_empty_and_whitespace_abstract_is_dropped(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    self._rec("Empty", "", "10.1/e"),
                    self._rec("Whitespace", "   ", "10.1/w"),
                    self._rec("Kept", "Kept abstract.", "10.1/k"),
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert [it.title for it in items] == ["Kept"]

    def test_overfetch_keeps_limit_after_filtering(self) -> None:
        """Filtering null-abstract records must not shrink the result below
        ``limit`` when the over-fetched page contains enough real abstracts."""
        results = [
            self._rec(f"Editorial {i}", None, f"10.1/ed{i}") for i in range(3)
        ] + [
            self._rec(f"Article {i}", f"Abstract {i}.", f"10.1/a{i}") for i in range(5)
        ]
        payload = {"resultList": {"result": results}}
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 4)
        assert len(items) == 4
        assert all(it.abstract for it in items)


class TestPMCJournal:
    """PMCConnector shares the EuropePMC API and the same journal bug."""

    def test_journal_from_journal_info(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "PMC article",
                        "abstractText": "Abstract",
                        "doi": "10.1234/pmcj",
                        "pubYear": "2024",
                        "pmcid": "PMC2222222",
                        "journalInfo": {
                            "journal": {
                                "title": (
                                    "Journal of clinical and translational science"
                                ),
                            },
                        },
                    },
                ],
            },
        }
        conn = PMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Journal of clinical and translational science"

    def test_falls_back_when_journal_info_missing(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "PMC no journal",
                        "abstractText": "Abstract",
                        "doi": "10.1234/pmcj2",
                        "pubYear": "2024",
                        "pmcid": "PMC3333333",
                    },
                ],
            },
        }
        conn = PMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "PMC"


class TestCrossrefVolumeIssuePages:
    """CrossrefConnector should extract volume/issue/pages."""

    def test_extracts_all_biblio(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "title": ["Crossref article"],
                        "DOI": "10.1234/cr",
                        "author": [{"given": "A", "family": "B"}],
                        "container-title": ["Test Journal"],
                        "volume": "42",
                        "issue": "3",
                        "page": "101-115",
                        "published-print": {"date-parts": [[2024]]},
                    },
                ],
            },
        }
        conn = CrossrefConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].volume == "42"
        assert items[0].issue == "3"
        assert items[0].pages == "101-115"

    def test_missing_biblio_fields(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "title": ["No biblio"],
                        "DOI": "10.1234/nobiblio",
                    },
                ],
            },
        }
        conn = CrossrefConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].volume == ""
        assert items[0].issue == ""
        assert items[0].pages == ""


class TestCyberLeninkaAuthors:
    """CyberLeninkaConnector should extract authors from rec['authors']."""

    def test_authors_from_list(self) -> None:
        payload = {
            "articles": [
                {
                    "name": "A long CyberLeninka article title for testing",
                    "annotation": "Abstract",
                    "link": "/article/123",
                    "year": "2023",
                    "journal": "CL Journal",
                    "authors": ["Alice Smith", "Bob Jones"],
                },
            ],
        }
        conn = CyberLeninkaConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Alice Smith", "Bob Jones")

    def test_no_authors_key(self) -> None:
        payload = {
            "articles": [
                {
                    "name": "Another long CyberLeninka article title for test",
                    "annotation": "Abstract",
                    "link": "/article/456",
                    "year": "2023",
                    "journal": "CL Journal",
                },
            ],
        }
        conn = CyberLeninkaConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ()


class TestCyberLeninkaAbstractFallback:
    """CyberLeninka ``enrich_raw`` backfills an empty abstract from the body.

    The search API leaves ``annotation`` empty for a minority of articles, and
    the landing page carries no abstract meta tag, so the inherited
    ``enrich_raw`` cannot recover it. The page renders the body inside
    ``div.ocr[itemprop="articleBody"]``, but that block is prepended with a
    recommended-article preview and interleaved with the bibliography, so the
    abstract must be located relative to the article's *own* title paragraph
    (matched fuzzily, since the page sometimes OCR-flattens ``й`` to ``и``),
    skipping keyword/affiliation lines, and stopping at the first numbered
    reference or journal citation.
    """

    _ARTICLE_HTML = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # Recommended-article preview with an unrelated title — must not be
        # matched as this article's title.
        "<p>Смотрите также: совершенно иная статья о квантовых вычислениях</p>"  # noqa: RUF001
        # Author line before the title.
        "<p>Михайлова М. В.</p>"  # noqa: RUF001
        # The article's own title, OCR-flattened (НЕИРО- instead of НЕЙРО-).
        "<p>НЕИРОМАТЕМАТИКА В МАШИННОМ ОБУЧЕНИИ</p>"  # noqa: RUF001
        # Keyword lines precede the abstract and must be skipped, not stop.
        "<p>Ключевые слова: нейроматематика, машинное обучение, граф</p>"
        "<p>Keywords: neuromathematics, machine learning, graph</p>"
        # The abstract prose run.
        "<p>К нейроматематике принято относить раздел вычислительной "  # noqa: RUF001
        "математики, связанный с разработкой методов решения задач.</p>"  # noqa: RUF001
        "<p>Если рассматривать нейрокомпьютеры как устройства переработки "
        "информации, возникают вопросы о применимости таких систем.</p>"  # noqa: RUF001
        # Numbered reference list ends the run.
        "<p>1)\tу рассматриваемой задачи отсутствует алгоритм решения</p>"  # noqa: RUF001
        # Journal citation would also end the run if reached.
        "<p>Вестник медицинского института. 2023. Том 13. № 2.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_AFFILIATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Особенности применения наноматериалов в строительстве</p>"
        # Affiliation line right after the title — short and must be skipped.
        "<p>Московский государственный университет, кафедра физики</p>"
        "<p>В работе рассмотрены особенности применения наноматериалов "  # noqa: RUF001
        "в современных строительных конструкциях и их долговечность.</p>"
        "<p>Вестник строительных наук. 2022. Том 8. № 1.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_NO_OCR = "<html><body><p>no article body block here</p></body></html>"

    _ARTICLE_HTML_PROSE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Машинное обучение в задачах классификации</p>"
        # Common Russian prose tokens that must NOT end the abstract run:
        # "in such cases", "i.e.", "etc.", "in one or another", "since".
        "<p>В работе рассматриваются методы машинного обучения, "  # noqa: RUF001
        "в том числе ансамблевые подходы, т.е. композиции моделей, "  # noqa: RUF001
        "и т.д., применяемые т.к. они устойчивы к переобучению.</p>"
        "<p>Показано, что в том или ином случае метод сходится.</p>"
        "<p>Вестник информатики. 2023. Том 13. № 2.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_RELATED_PREVIEW = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # A related-article preview sharing ≥60% of the title's tokens — must
        # be rejected as the title via the preview-prefix guard.
        "<p>Смотрите также: глубокое машинное обучение в медицинских "
        "задачах</p>"
        "<p>Машинное обучение в диагностике заболеваний</p>"
        "<p>Ключевые слова: машинное обучение, диагностика</p>"
        "<p>В работе машинное обучение применяется к диагностике "  # noqa: RUF001
        "заболеваний на ранних стадиях.</p>"
        "<p>Вестник медицинского журнала. 2022. Том 5. № 1.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_PREVIEW_VARIANT = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # An unlisted preview prefix sharing ≥60% of the title's tokens — must
        # be rejected via the expanded preview-prefix guard.
        "<p>Также в этом номере: методы оптимизации в логистике предприятия"
        "</p>"
        "<p>Методы оптимизации в логистике</p>"
        "<p>В работе предложен метод оптимизации маршрутов доставки.</p>"  # noqa: RUF001
        "<p>Том 7. № 2.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_SQUARE_REFS = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Методы оптимизации в логистике</p>"
        # Bare author line (surname + two initials, no institution) before the
        # abstract run — must be skipped via the author-initial guard.
        "<p>Иванов И.И., Петров П.П.</p>"
        "<p>В работе предложен метод оптимизации маршрутов доставки.</p>"  # noqa: RUF001
        "<p>Эксперименты показали снижение издержек на 15 процентов.</p>"
        # Bibliography heading variant — must end the abstract run.
        "<p>Список использованной литературы</p>"
        # Square-bracket references after the header must not be collected.
        "<p>[1] Смирнов А.А. Логистика. М.: Наука, 2020.</p>"  # noqa: RUF001
        "<p>[2] Козлов В.В. Оптимизация. СПб.: Питер, 2019.</p>"  # noqa: RUF001
        "</div></body></html>"
    )

    _ARTICLE_HTML_SHORT_OCR_TITLE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # Body title OCR-flattened (й→и); a short single-token title whose only
        # differing token is the й-bearing one — only the й↔и fold rescues it.
        "<p>Неиросети</p>"
        "<p>Нейросетевые модели описаны кратко в данной работе.</p>"
        "<p>Том 3. № 1.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_PREVIEW_VYPUSK = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # An "in this issue" preview whose normalized text embeds the title as
        # a substring — without the "в этом выпуске" prefix guard the substring
        # branch of _find_cyberleninka_title would match the preview as the
        # title, so the real title would be collected as the abstract opener.
        "<p>В этом выпуске: методы оптимизации в логистике предприятия</p>"  # noqa: RUF001
        "<p>Методы оптимизации в логистике</p>"
        "<p>В работе предложен метод оптимизации маршрутов доставки.</p>"  # noqa: RUF001
        "<p>Том 7. № 2.</p>"
        "</div></body></html>"
    )

    def test_abstract_extracted_from_body_after_ocr_title(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Нейроматематика в машинном обучении",
            url="https://cyberleninka.ru/article/n/neyromatematika",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        assert "вычислительной математики" in enriched.abstract
        assert "нейрокомпьютеры" in enriched.abstract
        # The recommended-article preview must not leak in.
        assert "квантовых" not in enriched.abstract
        assert "Смотрите также" not in enriched.abstract
        # The author line precedes the title and must not be collected.
        assert "Михайлова" not in enriched.abstract
        # Keyword lines must be skipped, not appear in the abstract.
        assert "Ключевые слова" not in enriched.abstract
        assert "Keywords" not in enriched.abstract
        # The numbered reference must end the run, not be collected.
        assert "отсутствует алгоритм" not in enriched.abstract
        assert "1)" not in enriched.abstract
        # The journal citation must not leak in.
        assert "Том 13" not in enriched.abstract

    def test_affiliation_line_after_title_is_skipped(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_AFFILIATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Особенности применения наноматериалов в строительстве",
            url="https://cyberleninka.ru/article/n/nanomaterials",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        assert "наноматериалов" in enriched.abstract
        assert "долговечность" in enriched.abstract
        # The affiliation line must be skipped, not collected.
        assert "кафедра" not in enriched.abstract
        assert "университет" not in enriched.abstract
        # The journal citation must end the run.
        assert "Том 8" not in enriched.abstract

    def test_nonempty_api_abstract_skips_fallback(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        # The inherited ``enrich_raw`` always fetches the landing page once
        # (base.py), so the stub must return real body HTML rather than raise.
        # The contract under test is narrower: the *fallback* extraction must
        # not run — i.e. it must not overwrite the API abstract with body prose.
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="A real article with a full abstract already",
            url="https://cyberleninka.ru/article/n/has-abstract",
            abstract="A genuine abstract already provided by the search API.",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The API abstract is preserved verbatim (normalised); the fallback
        # body extraction never overwrites a real abstract.
        assert (
            enriched.abstract
            == "A genuine abstract already provided by the search API."
        )
        # Body prose the fallback would have extracted is absent, proving the
        # fallback path was not taken even though the page carries it.
        assert "вычислительной" not in enriched.abstract

    def test_no_ocr_block_leaves_abstract_empty(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_NO_OCR,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Some article whose landing page lacks the body block",
            url="https://cyberleninka.ru/article/n/no-body",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.abstract == ""

    def test_request_failure_leaves_abstract_empty(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()

        def _raise(_url: str, **_kwargs: object) -> str:
            raise RuntimeError("sidecar 502")

        monkeypatch.setattr(conn, "_request_text", _raise)
        raw = RawArticle(
            source_key="cyberleninka",
            title="Some article whose landing page request fails",
            url="https://cyberleninka.ru/article/n/fails",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # A failed fallback fetch must degrade to the raw (empty) abstract,
        # not abort enrichment.
        assert enriched.abstract == ""

    def test_common_russian_prose_not_truncated(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PROSE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Машинное обучение в задачах классификации",
            url="https://cyberleninka.ru/article/n/prose",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # Ordinary Russian prose tokens (in-such-cases, i.e., etc.,
        # in-one-or-another, since) must not truncate the abstract run.
        assert "в том числе" in enriched.abstract
        assert "композиции моделей" in enriched.abstract
        assert "метод сходится" in enriched.abstract
        # The journal citation line still ends the run.
        assert "Том 13" not in enriched.abstract
        assert "Вестник" not in enriched.abstract

    def test_related_preview_not_matched_as_title(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_RELATED_PREVIEW,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Машинное обучение в диагностике заболеваний",
            url="https://cyberleninka.ru/article/n/related-preview",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The related-article preview must not be matched as the title, so
        # its content and the title/keyword lines must not leak into the
        # abstract.
        assert "применяется к диагностике" in enriched.abstract
        assert "Смотрите также" not in enriched.abstract
        assert "глубокое" not in enriched.abstract
        assert "Ключевые слова" not in enriched.abstract
        assert "Том 5" not in enriched.abstract

    def test_unlisted_preview_prefix_rejected(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PREVIEW_VARIANT,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Методы оптимизации в логистике",
            url="https://cyberleninka.ru/article/n/preview-variant",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The unlisted preview prefix must be rejected, so its content and the
        # title line must not leak into the abstract; only the real abstract
        # run is collected.
        assert "маршрутов доставки" in enriched.abstract
        assert "Также в этом номере" not in enriched.abstract
        assert "предприятия" not in enriched.abstract
        assert "Том 7" not in enriched.abstract

    def test_square_bracket_refs_and_author_line_stopped(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_SQUARE_REFS,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Методы оптимизации в логистике",
            url="https://cyberleninka.ru/article/n/square-refs",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The bare author line must be skipped, not collected.
        assert "Иванов" not in enriched.abstract
        assert "Петров" not in enriched.abstract
        # The abstract prose run is collected.
        assert "маршрутов доставки" in enriched.abstract
        assert "снижение издержек" in enriched.abstract
        # The bibliography heading variant must end the run, not be collected.
        assert "Список использованной" not in enriched.abstract
        # Square-bracket references must not bleed in.
        assert "Смирнов" not in enriched.abstract
        assert "Козлов" not in enriched.abstract
        assert "[1]" not in enriched.abstract

    def test_short_ocr_title_matched_via_fold(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_SHORT_OCR_TITLE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Нейросети",
            url="https://cyberleninka.ru/article/n/short-ocr",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # A short single-token OCR-flattened title is only rescued by the
        # й↔и fold; without it the title would not match and the abstract
        # would be empty.
        assert "Нейросетевые модели" in enriched.abstract
        assert "в данной работе" in enriched.abstract
        assert "Том 3" not in enriched.abstract

    def test_vypusk_preview_not_matched_as_title(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PREVIEW_VYPUSK,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Методы оптимизации в логистике",
            url="https://cyberleninka.ru/article/n/vypusk-preview",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The "в этом выпуске" preview embeds the title as a substring, so
        # without the prefix guard the substring branch would match the
        # preview as the title; the preview content and the real title must
        # not leak into the abstract.
        assert "маршрутов доставки" in enriched.abstract
        assert "В этом выпуске" not in enriched.abstract  # noqa: RUF001
        assert "предприятия" not in enriched.abstract
        assert "Том 7" not in enriched.abstract

    _ARTICLE_HTML_UNIVERSITY_OPENER = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # An abstract opener that mentions a university and a finite-verb
        # stem ("предложен") -- without the verb-stem guard the affiliation
        # skip would drop it as an affiliation line, losing the whole run.
        "<p>Особенности применения наноматериалов в строительстве</p>"
        "<p>В Томском государственном университете предложен новый метод "  # noqa: RUF001
        "повышения долговечности конструкций.</p>"
        "<p>Вестник строительных наук. 2023. Том 7. № 2.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_INLINE_ISSUE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Методы оптимизации в логистике</p>"
        "<p>В работе предложен метод оптимизации маршрутов доставки.</p>"  # noqa: RUF001
        # A prose sentence mentioning an inline "в таблице № 3" must not be
        # truncated by a bare issue-sign guard; only short header-like lines
        # carrying "№ N" are treated as a stop.
        "<p>Метод апробирован на данных из таблицы № 3 и показал "
        "эффективность 15 процентов.</p>"
        "<p>Результаты подтвердили результат эксперимента.</p>"
        "<p>Вестник логистики. 2023. Том 7. № 2.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_UNLISTED_LONG_PREVIEW = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # An unlisted preview prefix ("в новом выпуске" is not in the prefix
        # set) that embeds the title as a substring and is much longer than
        # the title. Without the loose substring branch and the overlap
        # length cap, this preview would be matched as the title.
        "<p>В новом выпуске журнала: машинное обучение в медицине "  # noqa: RUF001
        "и смежные области</p>"
        "<p>Машинное обучение в медицине</p>"
        "<p>В работе машинное обучение применяется к диагностике "  # noqa: RUF001
        "заболеваний на ранних стадиях.</p>"
        "<p>Том 5. № 1.</p>"
        "</div></body></html>"
    )

    def test_opener_mentioning_university_is_kept(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_UNIVERSITY_OPENER,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Особенности применения наноматериалов в строительстве",
            url="https://cyberleninka.ru/article/n/university-opener",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The university opener carries a finite-verb stem, so it must be
        # kept as prose rather than skipped as an affiliation line.
        assert "предложен новый метод" in enriched.abstract
        assert "университете" in enriched.abstract
        assert "долговечности" in enriched.abstract
        assert "Том 7" not in enriched.abstract

    def test_prose_with_inline_issue_sign_not_truncated(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_INLINE_ISSUE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Методы оптимизации в логистике",
            url="https://cyberleninka.ru/article/n/inline-issue",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The inline "в таблице № 3" sits in a long prose sentence, so it
        # must not truncate the run; the surrounding sentences are kept.
        assert "таблицы" in enriched.abstract
        assert "эффективность" in enriched.abstract
        assert "подтвердили результат" in enriched.abstract
        assert "Том 7" not in enriched.abstract

    def test_unlisted_long_preview_not_matched_as_title(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_UNLISTED_LONG_PREVIEW,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Машинное обучение в медицине",
            url="https://cyberleninka.ru/article/n/unlisted-long-preview",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The unlisted long preview embeds the title as a substring but is too
        # long for the overlap cap, so it is not matched as the title and its
        # content does not leak into the abstract.
        assert "применяется к диагностике" in enriched.abstract
        assert "в новом выпуске" not in enriched.abstract
        assert "смежные области" not in enriched.abstract
        assert "Том 5" not in enriched.abstract

    _ARTICLE_HTML_PROVEDEN_OPENER = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        # An opener with a stem ("проведён") added in round-5 -- before the
        # addition it was wrongly skipped as an affiliation line.
        "<p>Анализ геномных данных</p>"
        "<p>В Новосибирском государственном университете проведён анализ "  # noqa: RUF001
        "геномных данных различных популяций.</p>"
        "<p>Вестник геномики. 2024. Том 3. № 1.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_ISSUE_ONLY_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Новые методы в биоинформатике</p>"
        "<p>В работе проведён анализ последовательностей ДНК.</p>"  # noqa: RUF001
        # An issue-only citation (no volume/ISSN/UDC/DOI) longer than the old
        # 40-char cap -- must still stop via the terminal issue-sign guard.
        "<p>Известия высших учебных заведений. Поволжский регион. № 3.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_ISSUE_THEN_YEAR = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Квантовые вычисления</p>"
        "<p>В работе исследованы алгоритмы квантовой оптимизации.</p>"  # noqa: RUF001
        # A citation ending in "№ N. <year>" -- the trailing year after the
        # issue sign must still count as terminal.
        "<p>Успехи физических наук. № 12. 2023.</p>"
        "</div></body></html>"
    )

    def test_proven_opener_with_university_is_kept(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PROVEDEN_OPENER,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Анализ геномных данных",
            url="https://cyberleninka.ru/article/n/proven-opener",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The "проведён" stem (added in round-5) keeps the university opener
        # as prose instead of skipping it as an affiliation line.
        assert "проведён анализ" in enriched.abstract
        assert "Новосибирском" in enriched.abstract
        assert "геномных данных различных" in enriched.abstract
        assert "Том 3" not in enriched.abstract

    def test_issue_only_long_citation_still_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_ISSUE_ONLY_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Новые методы в биоинформатике",
            url="https://cyberleninka.ru/article/n/issue-only-citation",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The prose run is collected; the issue-only citation line (longer
        # than the old 40-char cap, with no volume marker) still stops via
        # the terminal issue-sign guard and does not leak.
        assert "проведён анализ" in enriched.abstract
        assert "последовательностей" in enriched.abstract
        assert "Известия" not in enriched.abstract
        assert "Поволжский регион" not in enriched.abstract

    def test_issue_sign_followed_by_year_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_ISSUE_THEN_YEAR,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Квантовые вычисления",
            url="https://cyberleninka.ru/article/n/issue-then-year",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # A citation whose issue sign is followed by a trailing year still
        # counts as terminal and stops the run.
        assert "исследованы алгоритмы" in enriched.abstract
        assert "квантовой оптимизации" in enriched.abstract
        assert "Успехи физических" not in enriched.abstract
        assert "Том" not in enriched.abstract

    _ARTICLE_HTML_AFFILIATION_THEN_PROSE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Название исследования</p>"
        # A pre-abstract affiliation line ending in a terminal issue sign: the
        # affiliation skip runs BEFORE the terminal-issue stop, so the line is
        # skipped rather than ending the run with an empty abstract.
        "<p>Кафедра физики № 7.</p>"
        "<p>В работе исследованы новые материалы.</p>"  # noqa: RUF001
        "</div></body></html>"
    )

    _ARTICLE_HTML_PAGE_RANGE_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Анализ численных методов</p>"
        "<p>В работе проведён анализ численных методов.</p>"  # noqa: RUF001
        # An issue-only citation whose tail is a page range ("15-30") must
        # still count as terminal after the page/year markers are stripped.
        "<p>Журнал прикладной математики. № 12. 15-30.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_PAGES_PREFIX_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Изучение полимеров</p>"
        "<p>В статье изучены свойства полимеров.</p>"  # noqa: RUF001
        # A Russian page-letter marker before the page range must be stripped
        # so the remaining "5-15" still counts as a bibliographic tail.
        "<p>Физика твёрдого тела. № 12. С. 5-15.</p>"  # noqa: RUF001
        "</div></body></html>"
    )

    _ARTICLE_HTML_YEAR_SUFFIX_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Наноструктуры материалов</p>"
        "<p>В работе исследованы наноструктуры.</p>"  # noqa: RUF001
        # A trailing Russian year-letter marker after the issue sign must be
        # stripped so the remaining year still counts as a bibliographic tail.
        "<p>Нанотехнологии в России. № 12. 2023 г.</p>"  # noqa: RUF001
        "</div></body></html>"
    )

    _ARTICLE_HTML_PROSE_FINAL_ISSUE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Анализ экспериментальных данных</p>"
        # A prose sentence ending with an inline issue reference ("в таблицу
        # № 5") glued to a preceding word must NOT be truncated: the sign does
        # not open the line or follow a period, so it is not terminal.
        "<p>В работе исследованы данные и помещены в таблицу № 5. Результаты "  # noqa: RUF001
        "подтвердили гипотезу.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_INLINE_THEN_TRAILING_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Анализ новых материалов</p>"
        "<p>В работе исследованы свойства материалов.</p>"  # noqa: RUF001
        # An inline "№ 3" glued to "таблице" followed by a trailing citation
        # "Вестник. № 12. 2023.": the LAST issue sign is considered, so the
        # trailing citation stops the run instead of latching onto the inline
        # sign and leaking.
        "<p>В таблице № 3 показаны итоги. Вестник. № 12. 2023.</p>"  # noqa: RUF001
        "</div></body></html>"
    )

    def test_affiliation_ending_in_terminal_issue_is_skipped(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_AFFILIATION_THEN_PROSE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Название исследования",
            url="https://cyberleninka.ru/article/n/affiliation-issue",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The affiliation line ending in a terminal "№ 7" is skipped (the
        # affiliation skip runs before the terminal-issue stop), so the run
        # does not end empty and the following prose is collected.
        assert "исследованы" in enriched.abstract
        assert "Кафедра" not in enriched.abstract
        assert enriched.abstract

    def test_citation_tail_page_range_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PAGE_RANGE_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Анализ численных методов",
            url="https://cyberleninka.ru/article/n/page-range",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The page-range tail "15-30" after the issue sign is a bibliographic
        # tail, so the citation stops and does not leak into the abstract.
        assert "проведён анализ" in enriched.abstract
        assert "Журнал" not in enriched.abstract
        assert "15-30" not in enriched.abstract

    def test_citation_tail_pages_prefix_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PAGES_PREFIX_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Изучение полимеров",
            url="https://cyberleninka.ru/article/n/pages-prefix",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The page-letter marker is stripped, leaving the "5-15" range as a
        # bibliographic tail, so the citation stops and does not leak.
        assert "изучены" in enriched.abstract
        assert "Физика" not in enriched.abstract
        assert "5-15" not in enriched.abstract

    def test_citation_tail_year_marker_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_YEAR_SUFFIX_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Наноструктуры материалов",
            url="https://cyberleninka.ru/article/n/year-suffix",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The trailing year-letter marker is stripped, leaving "2023" as a
        # bibliographic tail, so the citation stops and does not leak.
        assert "работе" in enriched.abstract
        assert "2023" not in enriched.abstract
        assert "№ 12" not in enriched.abstract

    def test_prose_final_inline_issue_not_truncated(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PROSE_FINAL_ISSUE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Анализ экспериментальных данных",
            url="https://cyberleninka.ru/article/n/prose-final-issue",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The inline "в таблицу № 5" is glued to a preceding word (no period
        # before the sign), so it is not terminal and the sentence is kept in
        # full, including the text after the issue sign.
        assert "таблицу" in enriched.abstract
        assert "Результаты" in enriched.abstract
        assert "подтвердили" in enriched.abstract

    def test_inline_then_trailing_citation_stops_at_tail(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_INLINE_THEN_TRAILING_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Анализ новых материалов",
            url="https://cyberleninka.ru/article/n/inline-then-trailing",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The LAST issue sign (the trailing "№ 12") is considered, so the
        # trailing citation stops the run; the inline "№ 3" does not let the
        # "Вестник. № 12. 2023." citation leak into the abstract.
        assert "материалов" in enriched.abstract
        assert "2023" not in enriched.abstract
        assert "№ 12" not in enriched.abstract

    _ARTICLE_HTML_COMMA_BEFORE_ISSUE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Методы оптимизации систем</p>"
        "<p>В работе исследованы методы оптимизации.</p>"  # noqa: RUF001
        # A comma before the issue sign (``Серия 5, № 4``) is a citation
        # separator, so the sign is terminal and the citation stops the run;
        # the year in the tail must not leak into the abstract.
        "<p>Вестник. Серия 5, № 4. 2023.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_PAREN_YEAR_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Моделирование процессов</p>"
        "<p>В статье построены модели процессов.</p>"  # noqa: RUF001
        # A parenthesized year after the issue sign is a bibliographic tail
        # (parentheses are stripped), so the citation stops and does not leak.
        "<p>Вестник. № 4 (2023).</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_ABBREV_ISSUE_PROSE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Анализ экспериментальных данных</p>"
        "<p>В работе исследованы данные и сведены в таблицу.</p>"  # noqa: RUF001
        # A short prose abbreviation (``табл.``) ending in a period right before
        # the issue sign is a reference, not a citation separator, so the sign
        # is NOT terminal and the sentence is kept as abstract prose.
        "<p>Данные представлены в табл. № 3.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_EMDASH_YEAR_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Новые композиционные материалы</p>"
        "<p>В работе изучены новые материалы.</p>"  # noqa: RUF001
        # An em dash before the trailing year is a citation separator, and the
        # year after it is a bibliographic tail, so the citation stops.
        "<p>Вестник. № 12 — 2023.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_STR_MARKER_CITATION = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Численные методы анализа</p>"
        "<p>В работе описаны численные методы.</p>"  # noqa: RUF001
        # A lowercase Russian page marker (``стр.``) before the page range is
        # stripped, leaving "15-30" as a bibliographic tail, so the citation
        # stops and the range does not leak.
        "<p>Журнал вычислительной математики. № 12. стр. 15-30.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_PREABSTRACT_TERMINAL_ISSUE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Исследование сложных систем</p>"
        # A bare terminal issue sign as the first body paragraph (before any
        # prose) is a pre-abstract metadata line, not the citation that ends
        # the abstract: it is skipped so the run reaches the following prose.
        "<p>№ 12. 2023.</p>"
        "<p>В работе исследованы сложные системы.</p>"  # noqa: RUF001
        "</div></body></html>"
    )

    _ARTICLE_HTML_ABBREV_DASH_ISSUE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Данные измерений</p>"
        "<p>В работе обработаны данные.</p>"  # noqa: RUF001
        # An em dash between the ``табл.`` abbreviation and the issue sign must
        # not erase the abbreviation: the trailing separator is stripped before
        # the token split, so ``табл`` stays the last token, the denylist fires,
        # and the sentence is kept as prose (not truncated at ``№ 3``).
        "<p>Данные в табл. — № 3.</p>"
        "</div></body></html>"
    )

    _ARTICLE_HTML_ALLSEP_ISSUE = (
        "<html><body>"
        '<div class="ocr" itemprop="articleBody">'
        "<p>Результаты эксперимента</p>"
        "<p>В работе получены новые данные.</p>"  # noqa: RUF001
        # An OCR-degenerate paragraph that is only a separator then an issue
        # sign (``— № 3``) makes ``before`` all-separator: the rsplit guard
        # skips the denylist and the separator check treats it as a terminal
        # citation, so the prior prose is kept and the discriminator must NOT
        # raise (``"".rsplit(maxsplit=1)`` is ``[]``).
        "<p>— № 3.</p>"
        "</div></body></html>"
    )

    def test_citation_comma_before_issue_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_COMMA_BEFORE_ISSUE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Методы оптимизации систем",
            url="https://cyberleninka.ru/article/n/comma-before-issue",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The comma before ``№ 4`` is a citation separator, so the citation is
        # terminal and stops; the year and series must not leak.
        assert "оптимизации" in enriched.abstract
        assert "Серия" not in enriched.abstract
        assert "2023" not in enriched.abstract

    def test_citation_paren_year_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PAREN_YEAR_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Моделирование процессов",
            url="https://cyberleninka.ru/article/n/paren-year",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The parenthesized year is a bibliographic tail, so the citation
        # stops and the year does not leak.
        assert "построены" in enriched.abstract
        assert "2023" not in enriched.abstract
        assert "(2023)" not in enriched.abstract

    def test_abbrev_issue_prose_kept(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_ABBREV_ISSUE_PROSE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Анализ экспериментальных данных",
            url="https://cyberleninka.ru/article/n/abbrev-issue-prose",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The ``табл.`` abbreviation before ``№ 3`` is a reference, not a
        # citation separator, so the sentence is kept as abstract prose and
        # not truncated at the issue sign.
        assert "представлены" in enriched.abstract
        assert "табл" in enriched.abstract

    def test_citation_emdash_year_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_EMDASH_YEAR_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Новые композиционные материалы",
            url="https://cyberleninka.ru/article/n/emdash-year",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The em dash before the year is a citation separator and the year is
        # a bibliographic tail, so the citation stops and the year does not leak.
        assert "изучены" in enriched.abstract
        assert "2023" not in enriched.abstract

    def test_citation_str_marker_stops(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_STR_MARKER_CITATION,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Численные методы анализа",
            url="https://cyberleninka.ru/article/n/str-marker",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The lowercase ``стр.`` marker is stripped, leaving "15-30" as a
        # bibliographic tail, so the citation stops and the range does not leak.
        assert "описаны" in enriched.abstract
        assert "стр" not in enriched.abstract
        assert "15-30" not in enriched.abstract

    def test_abbrev_dash_issue_prose_kept(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_ABBREV_DASH_ISSUE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Данные измерений",
            url="https://cyberleninka.ru/article/n/abbrev-dash-issue",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The em dash separating ``табл.`` from ``№ 3`` must not let the denylist
        # miss the abbreviation: the sentence is kept as prose, not truncated.
        assert "обработаны" in enriched.abstract
        assert "табл" in enriched.abstract

    def test_allsep_issue_does_not_crash(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_ALLSEP_ISSUE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Результаты эксперимента",
            url="https://cyberleninka.ru/article/n/allsep-issue",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        # Must not raise IndexError when ``before`` is all separator chars.
        enriched = conn.enrich_raw(raw)
        # The prior prose is kept; the degenerate ``— № 3`` line is terminal
        # and excluded, so the issue sign does not leak into the abstract.
        assert "получены" in enriched.abstract
        assert "№ 3" not in enriched.abstract

    def test_preabstract_terminal_issue_is_skipped(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = CyberLeninkaConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_PREABSTRACT_TERMINAL_ISSUE,
        )
        raw = RawArticle(
            source_key="cyberleninka",
            title="Исследование сложных систем",
            url="https://cyberleninka.ru/article/n/preabstract-terminal-issue",
            abstract="",
            full_text="",
            language="ru",
            year=None,
            doi="",
            journal="CL Journal",
        )
        enriched = conn.enrich_raw(raw)
        # The bare terminal ``№ 12`` before any prose is a pre-abstract
        # metadata line and is skipped, so the run reaches the following prose
        # instead of ending with an empty abstract.
        assert enriched.abstract
        assert "исследованы" in enriched.abstract
        assert "2023" not in enriched.abstract


class TestHALJournal:
    """HALConnector should extract journal from journalTitle_s."""

    def test_journal_from_field(self) -> None:
        payload = {
            "response": {
                "docs": [
                    {
                        "halId_s": "hal-123",
                        "title_s": ["HAL Article"],
                        "abstract_s": ["Abstract"],
                        "doiId_s": "10.1234/hal",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-123",
                        "authFullName_s": ["Alice Smith"],
                        "journalTitle_s": ["Journal du HAL"],
                    },
                ],
            },
        }
        conn = HALConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Journal du HAL"

    def test_journal_from_string_field(self) -> None:
        """HAL Solr returns journalTitle_s as a string, not a list.

        Indexing the string as a list would yield its first character ('J'),
        so the connector must accept either shape.
        """
        payload = {
            "response": {
                "docs": [
                    {
                        "halId_s": "hal-789",
                        "title_s": ["HAL Article"],
                        "abstract_s": ["Abstract"],
                        "doiId_s": "10.1234/hal2",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-789",
                        "authFullName_s": ["Alice Smith"],
                        "journalTitle_s": "Journal of Bioanalysis & Biomedicine",
                    },
                ],
            },
        }
        conn = HALConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Journal of Bioanalysis & Biomedicine"

    def test_fallback_to_hal(self) -> None:
        payload = {
            "response": {
                "docs": [
                    {
                        "halId_s": "hal-456",
                        "title_s": ["No Journal"],
                        "abstract_s": [],
                        "doiId_s": "",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-456",
                        "authFullName_s": ["Bob Jones"],
                    },
                ],
            },
        }
        conn = HALConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "HAL"

    def test_language_from_field(self) -> None:
        """language_s (ISO code) must override the "fr" profile default.

        HAL is a French repository but indexes many English-language works;
        the per-record ``language_s`` Solr field carries the true language.
        """
        payload = {
            "response": {
                "docs": [
                    {
                        "halId_s": "hal-en",
                        "title_s": ["English HAL Article"],
                        "abstract_s": ["Abstract"],
                        "doiId_s": "",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-en",
                        "authFullName_s": ["Alice Smith"],
                        "language_s": ["en"],
                    },
                    {
                        "halId_s": "hal-fr",
                        "title_s": ["Article HAL en français"],
                        "abstract_s": ["Résumé"],
                        "doiId_s": "",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-fr",
                        "authFullName_s": ["Bob Martin"],
                        "language_s": ["fr"],
                    },
                    {
                        "halId_s": "hal-none",
                        "title_s": ["Missing Language"],
                        "abstract_s": ["Abstract"],
                        "doiId_s": "",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-none",
                        "authFullName_s": ["Carol Lee"],
                    },
                ],
            },
        }
        conn = HALConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert [it.language for it in items] == ["en", "fr", "fr"]


class TestPubMedEfetch:
    """PubMedConnector._parse_efetch_abstracts should parse XML."""

    def test_labeled_abstract_parts(self) -> None:
        from apps.ingestion.connectors.api_connectors import PubMedConnector

        xml = (
            "<PubmedArticleSet>"
            "<PubmedArticle>"
            "<MedlineCitation>"
            "<PMID>99999</PMID>"
            "<Article>"
            "<Abstract>"
            '<AbstractText Label="BACKGROUND">Some background.</AbstractText>'
            '<AbstractText Label="RESULTS">Key findings.</AbstractText>'
            "</Abstract>"
            "</Article>"
            "</MedlineCitation>"
            "</PubmedArticle>"
            "</PubmedArticleSet>"
        )
        result = PubMedConnector._parse_efetch_abstracts(xml)
        assert "99999" in result
        assert "BACKGROUND: Some background." in result["99999"]
        assert "RESULTS: Key findings." in result["99999"]

    def test_multiple_articles(self) -> None:
        from apps.ingestion.connectors.api_connectors import PubMedConnector

        xml = (
            "<PubmedArticleSet>"
            "<PubmedArticle>"
            "<MedlineCitation><PMID>111</PMID>"
            "<Article><Abstract><AbstractText>First.</AbstractText></Abstract></Article>"
            "</MedlineCitation>"
            "</PubmedArticle>"
            "<PubmedArticle>"
            "<MedlineCitation><PMID>222</PMID>"
            "<Article><Abstract><AbstractText>Second.</AbstractText></Abstract></Article>"
            "</MedlineCitation>"
            "</PubmedArticle>"
            "</PubmedArticleSet>"
        )
        result = PubMedConnector._parse_efetch_abstracts(xml)
        assert result.get("111") == "First."
        assert result.get("222") == "Second."


class TestOpenAlexJournal:
    """OpenAlexConnector should extract journal from primary_location.source."""

    def test_journal_from_primary_location(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "title": "OpenAlex article",
                    "abstract_inverted_index": {".": [[0, 0]]},
                    "doi": "https://doi.org/10.1234/oa",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Alice Smith"}},
                    ],
                    "primary_location": {
                        "source": {"display_name": "Nature"},
                    },
                },
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Nature"

    def test_journal_from_best_oa_location(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W456",
                    "title": "OpenAlex OA article",
                    "abstract_inverted_index": {"Abstract": [0], "text": [1]},
                    "doi": "https://doi.org/10.1234/oa2",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Bob Jones"}},
                    ],
                    "best_oa_location": {
                        "source": {"display_name": "PLOS ONE"},
                    },
                },
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "PLOS ONE"

    def test_no_journal_when_missing(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W789",
                    "title": "OpenAlex no journal",
                    "abstract_inverted_index": {"Abstract": [0], "text": [1]},
                    "doi": "https://doi.org/10.1234/oa3",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Carol Lee"}},
                    ],
                },
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == ""


class TestOpenAlexAbstract:
    """OpenAlexConnector must drop works with a null/missing abstract.

    OpenAlex genuinely returns ``abstract_inverted_index: null`` for a sizeable
    share of works (verified live: 6/15 null for 'medical imaging', including
    the seminal 'A survey on deep learning in medical image analysis').
    Crossref lacks these abstracts too, so there is no enrichment fallback.
    Such null-abstract works are garbage for the search index, so they are
    filtered out and a larger page is requested so the result still reaches
    ``limit`` articles with a real abstract.
    """

    def _rec(self, title: str, inverted_index: object, doi: str) -> dict:
        rec = {
            "id": f"https://openalex.org/{doi}",
            "title": title,
            "doi": f"https://doi.org/{doi}",
            "publication_year": 2024,
            "authorships": [{"author": {"display_name": "An Author"}}],
            "primary_location": {"landing_page_url": "https://example.org/x"},
        }
        if inverted_index is not None:
            rec["abstract_inverted_index"] = inverted_index
        return rec

    def test_null_abstract_is_dropped(self) -> None:
        payload = {
            "results": [
                self._rec("Survey", None, "10.1/survey"),
                self._rec(
                    "Real Article",
                    {"A": [0], "real": [1], "abstract": [2]},
                    "10.1/a",
                ),
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].title == "Real Article"
        assert items[0].abstract == "A real abstract"

    def test_missing_abstract_key_is_dropped(self) -> None:
        """A work carrying no ``abstract_inverted_index`` key is also dropped."""
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/Wmissing",
                    "title": "No abstract key",
                    "doi": "https://doi.org/10.1/missing",
                    "publication_year": 2024,
                    "authorships": [{"author": {"display_name": "An Author"}}],
                    "primary_location": {
                        "landing_page_url": "https://example.org/y",
                    },
                },
                self._rec(
                    "Kept",
                    {"Kept": [0], "abstract": [1]},
                    "10.1/k",
                ),
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert [it.title for it in items] == ["Kept"]

    def test_overfetch_keeps_limit_after_filtering(self) -> None:
        """Filtering null-abstract works must not shrink the result below
        ``limit`` when the over-fetched page contains enough real abstracts."""
        results = [self._rec(f"Null {i}", None, f"10.1/n{i}") for i in range(3)] + [
            self._rec(f"Article {i}", {f"Abstract{i}": [0]}, f"10.1/a{i}")
            for i in range(5)
        ]
        payload = {"results": results}
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 4)
        assert len(items) == 4
        assert all(it.abstract for it in items)


class TestBrowserTransport:
    """``BrowserTransport`` proxies fetches to the cloakbrowser sidecar.

    ``Session.post`` is stubbed so no real HTTP is performed. The tests pin
    the contract connectors rely on: transient sidecar failures (network
    errors, 502/504) retry with backoff; upstream HTTP errors
    (``status >= 400`` returned in the 200 body) and non-retryable sidecar
    errors (422/other 4xx) raise :class:`ConnectorFetchError`.
    """

    @staticmethod
    def _make_transport(monkeypatch):
        from apps.ingestion.connectors import base

        monkeypatch.setattr(base.time, "sleep", lambda _seconds: None)
        return base.BrowserTransport(source_key="test", max_attempts=3)

    @staticmethod
    def _response(status_code, payload):
        class _Resp:
            def __init__(self) -> None:
                self.status_code = status_code

            def json(self):
                return payload

        return _Resp()

    def test_fetch_returns_decoded_text_body(self, monkeypatch) -> None:
        transport = self._make_transport(monkeypatch)
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(
                200,
                {
                    "status": 200,
                    "content_type": "text/html",
                    "encoding": "text",
                    "body": "<html>ok</html>",
                },
            ),
        )
        result = transport.fetch("https://example.org")
        assert result.status == 200
        assert result.body_text == "<html>ok</html>"
        assert result.body_bytes == b"<html>ok</html>"
        assert result.content_type == "text/html"

    def test_fetch_decodes_base64_body(self, monkeypatch) -> None:
        import base64

        transport = self._make_transport(monkeypatch)
        raw = b"%PDF-1.4 binary"
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(
                200,
                {
                    "status": 200,
                    "content_type": "application/pdf",
                    "encoding": "base64",
                    "body": base64.b64encode(raw).decode(),
                },
            ),
        )
        result = transport.fetch("https://example.org")
        assert result.body_bytes == raw
        assert result.body_text is None

    def test_upstream_http_error_raises(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(
                200,
                {
                    "status": 403,
                    "content_type": "text/html",
                    "encoding": "text",
                    "body": "<html>403</html>",
                },
            ),
        )
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")

    def test_retries_on_network_error_then_succeeds(self, monkeypatch) -> None:
        import requests

        transport = self._make_transport(monkeypatch)
        calls = {"n": 0}

        def _post(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("boom")
            return self._response(
                200,
                {
                    "status": 200,
                    "content_type": "text/html",
                    "encoding": "text",
                    "body": "ok",
                },
            )

        monkeypatch.setattr(transport._session, "post", _post)
        result = transport.fetch("https://example.org")
        assert result.body_text == "ok"
        assert calls["n"] == 2

    def test_retries_on_502_then_raises_after_max_attempts(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(502, {}),
        )
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")

    def test_sidecar_422_raises_without_retry(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        calls = {"n": 0}

        def _post(*a, **k):
            calls["n"] += 1
            return self._response(422, {})

        monkeypatch.setattr(transport._session, "post", _post)
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")
        assert calls["n"] == 1

    def test_sidecar_200_non_json_body_raises(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        calls = {"n": 0}

        def _post(*a, **k):
            calls["n"] += 1

            class _Resp:
                status_code = 200

                def json(self):
                    msg = "non-JSON body"
                    raise ValueError(msg)

            return _Resp()

        monkeypatch.setattr(transport._session, "post", _post)
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")
        # ValueError is a hard failure, not a transient retry trigger.
        assert calls["n"] == 1

    def test_post_json_forwards_json_body(self, monkeypatch) -> None:
        transport = self._make_transport(monkeypatch)
        captured = {}

        def _post(endpoint, json=None, timeout=None):
            captured["payload"] = json
            return self._response(
                200,
                {
                    "status": 200,
                    "content_type": "application/json",
                    "encoding": "text",
                    "body": "{}",
                },
            )

        monkeypatch.setattr(transport._session, "post", _post)
        transport.post_json("https://example.org/api", {"q": "math"})
        assert captured["payload"]["method"] == "POST"
        assert captured["payload"]["json"] == {"q": "math"}
        assert captured["payload"]["url"] == "https://example.org/api"


class TestSciELOOaiJournal:
    """_clean_oai_journal strips the volume/issue/year tail from dc:source.

    SciELO OAI records encode the venue as ``<journal> v.24 n.4 2017``;
    the volume, issue and year must not leak into the ``journal`` field.
    """

    def test_strips_v_n_year_tokens(self) -> None:
        assert (
            SciELOConnector._clean_oai_journal(
                "Revista de la Sociedad Española del Dolor v.24 n.4 2017",
            )
            == "Revista de la Sociedad Española del Dolor"
        )

    def test_strips_vol_no_tokens(self) -> None:
        assert (
            SciELOConnector._clean_oai_journal("Some Journal vol.10 no.2 2020")
            == "Some Journal"
        )

    def test_preserves_plain_journal(self) -> None:
        assert SciELOConnector._clean_oai_journal("Plain Journal") == "Plain Journal"

    def test_empty_returns_empty(self) -> None:
        assert SciELOConnector._clean_oai_journal("") == ""


class TestSciELORssAuthors:
    """_split_rss_authors parses SciELO RSS ``<author>`` semicolon lists.

    SciELO RSS author blobs are ``;``-separated ``Last, First`` pairs; the
    inner comma must not split a name, and the parsed list must be stored on
    the RawArticle (previously parsed then discarded).
    """

    def test_splits_semicolon_list_preserving_inner_commas(self) -> None:
        blob = (
            "Cervantes-Guerrero, Mario Daniel; Galván-Tejada, Carlos E.; Cruz, Miguel"
        )
        assert SciELOConnector._split_rss_authors(blob) == (
            "Cervantes-Guerrero, Mario Daniel",
            "Galván-Tejada, Carlos E.",
            "Cruz, Miguel",
        )

    def test_single_name_without_semicolon(self) -> None:
        assert SciELOConnector._split_rss_authors("Adnan Muhisn, Sinan") == (
            "Adnan Muhisn, Sinan",
        )

    def test_dedupes_preserving_order(self) -> None:
        blob = "Smith, J.; Jones, A.; Smith, J."
        assert SciELOConnector._split_rss_authors(blob) == ("Smith, J.", "Jones, A.")

    def test_empty_returns_empty_tuple(self) -> None:
        assert SciELOConnector._split_rss_authors("") == ()
        assert SciELOConnector._split_rss_authors("   ;  ; ") == ()


class TestSciELOQueryTerms:
    """_query_terms normalizes a query into matchable tokens."""

    def test_lowercases_and_splits(self) -> None:
        assert SciELOConnector._query_terms("Machine Learning 2024") == [
            "machine",
            "learning",
            "2024",
        ]

    def test_drops_short_tokens(self) -> None:
        assert SciELOConnector._query_terms("a of ml") == []

    def test_empty_query_returns_empty(self) -> None:
        assert SciELOConnector._query_terms("") == []


class TestSciELOArticleMatchesTerms:
    """_article_matches_terms checks query terms against article fields."""

    def test_matches_title(self) -> None:
        from apps.ingestion.connectors.base import RawArticle

        art = RawArticle(
            source_key="scielo",
            title="Machine learning approaches to imaging",
            url="https://x",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="",
        )
        assert SciELOConnector._article_matches_terms(art, ["machine", "learning"])

    def test_no_match_for_irrelevant(self) -> None:
        from apps.ingestion.connectors.base import RawArticle

        art = RawArticle(
            source_key="scielo",
            title="Acupuntura para el dolor de rodilla",
            url="https://x",
            abstract="",
            full_text="",
            language="es",
            year=None,
            doi="",
            journal="",
        )
        assert not SciELOConnector._article_matches_terms(
            art,
            ["machine", "learning"],
        )

    def test_partial_match_is_rejected(self) -> None:
        """AND semantics: a single shared token must not pass a 2-term query.

        ``learning by doing`` and ``Blended learning`` share ``learning``
        with ``machine learning`` but are not about ML; the matcher must
        require both terms.
        """
        from apps.ingestion.connectors.base import RawArticle

        art = RawArticle(
            source_key="scielo",
            title="A relationship between external public debt and economic growth",
            url="https://x",
            abstract="An endogenous growth model with learning by doing.",
            full_text="",
            language="en",
            year=2015,
            doi="",
            journal="Estudios Económicos",
        )
        assert not SciELOConnector._article_matches_terms(
            art,
            ["machine", "learning"],
        )


class TestSciELOOaiFetch:
    """_fetch_oai post-filters records by query terms and cleans journal.

    OAI-PMH ``ListRecords`` is date-based; before the fix the SciELO
    fallback returned the first N records by date regardless of the
    query, emitting query-irrelevant articles (e.g. acupuncture papers
    for a ``machine learning`` search) with the volume/issue/year tail
    fused into ``journal``. The fallback must keep only records whose
    text fields contain a query term and strip the venue tail.
    """

    _OAI_XML = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" '
        'xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<ListRecords>"
        "<record><metadata><oai_dc:dc>"
        "<dc:title>Acupuntura para el dolor de rodilla</dc:title>"
        "<dc:identifier>https://www.scielo.br/a/acupuntura</dc:identifier>"
        "<dc:source>Revista Dolor v.24 n.4 2017</dc:source>"
        "<dc:date>2017-01-01</dc:date>"
        "</oai_dc:dc></metadata></record>"
        "<record><metadata><oai_dc:dc>"
        "<dc:title>Machine learning approaches to medical imaging</dc:title>"
        "<dc:identifier>https://www.scielo.br/a/ml-imaging</dc:identifier>"
        "<dc:source>Computer Methods v.1 n.2 2024</dc:source>"
        "<dc:date>2024-01-01</dc:date>"
        "</oai_dc:dc></metadata></record>"
        "</ListRecords></OAI-PMH>"
    )

    def test_filters_irrelevant_and_cleans_journal(self, monkeypatch) -> None:
        conn = SciELOConnector()

        monkeypatch.setattr(conn, "_request_xml_text", lambda _url: self._OAI_XML)

        items = conn._fetch_oai("machine learning", 3)
        assert len(items) == 1
        assert "Machine learning" in items[0].title
        assert items[0].journal == "Computer Methods"
        assert items[0].year == 2024

    def test_empty_query_keeps_all(self, monkeypatch) -> None:
        conn = SciELOConnector()

        monkeypatch.setattr(conn, "_request_xml_text", lambda _url: self._OAI_XML)

        items = conn._fetch_oai("", 3)
        assert len(items) == 2

    def test_no_relevant_raises(self, monkeypatch) -> None:

        from apps.ingestion.connectors.base import ConnectorFetchError

        conn = SciELOConnector()

        monkeypatch.setattr(conn, "_request_xml_text", lambda _url: self._OAI_XML)

        with pytest.raises(ConnectorFetchError):
            conn._fetch_oai("quantum computing", 3)

    def test_oai_fails_over_to_next_mirror(self, monkeypatch) -> None:
        """A transport error on the first mirror falls through to the next.

        ``_request_xml_text`` translates network errors into
        ``ConnectorFetchError``. ``_fetch_oai`` must catch that exception per
        endpoint and move on to the next mirror rather than aborting the
        whole fetch — a read timeout on ``scielo.isciii.es`` must surface
        records from ``scielo.org.mx``.
        """
        from apps.ingestion.connectors.base import ConnectorFetchError

        conn = SciELOConnector()
        isciii, mexico = conn.OA_MIRRORS

        def stub(url: str) -> str:
            if url.startswith(isciii):
                msg = "scielo: oai transport error: simulated read timeout"
                raise ConnectorFetchError(msg)
            if url.startswith(mexico):
                return self._OAI_XML
            msg = f"unexpected url: {url}"
            raise AssertionError(msg)

        monkeypatch.setattr(conn, "_request_xml_text", stub)

        items = conn._fetch_oai("machine learning", 3)
        assert len(items) == 1
        assert "Machine learning" in items[0].title
        assert items[0].journal == "Computer Methods"


class TestSciELOArticleMetaEnrich:
    """enrich_raw pulls journal/DOI/abstract/authors from the ArticleMeta API.

    SciELO RSS hardcodes ``journal="SciELO"`` (RSS has no journal field) and
    carries no DOI, and the article landing page sits behind a BunnyCDN
    interstitial the sidecar cannot clear, so the inherited ``enrich_raw``
    page fetch raised ``ConnectorFetchError`` and broke ingestion. The override
    fetches ArticleMeta JSON instead and never touches the article page; on
    API failure it returns the RSS payload unchanged (graceful, no raise).
    """

    _ARTICLEMETA: ClassVar[dict[str, object]] = {
        "doi": "10.61286/E-RMS.V4I.381",
        "publication_year": "2026",
        "title": {"v100": [{"_": "e-Revista Multidisciplinaria del Saber"}]},
        "article": {
            "v40": [{"_": "es"}],
            "v12": [{"l": "es", "_": "Titulo en espanol"}],
            "v83": [
                {
                    "l": "es",
                    "a": "Resumen  Introduccion. Estudio sobre diabetes.",
                    "_": "",
                },
                {"l": "en", "a": "Abstract  Introduction. Study on diabetes.", "_": ""},
            ],
            "v10": [
                {
                    "s": "Cervantes-Guerrero",
                    "n": "Mario Daniel",
                    "k": "0000-0003-2754-5388",
                },
                {"s": "Galvan-Tejada", "n": "Carlos E.", "k": "0000-0002-7635-4687"},
            ],
            "v237": [{"_": "10.61286/e-rms.v4i.381"}],
        },
    }

    def _raw(self, **overrides) -> object:
        from apps.ingestion.connectors.base import RawArticle

        defaults = {
            "source_key": "scielo",
            "title": "Explainable AI for diabetes",
            "url": "https://search.scielo.org/resource/pt/S2960-24672026000102026-ven",
            "abstract": "Resumen Estudio sobre diabetes.",
            "full_text": (
                "Explainable AI for diabetes Resumen Estudio sobre diabetes. SciELO"
            ),
            "language": "es",
            "year": 2026,
            "doi": "",
            "journal": "SciELO",
            "authors": ("Cervantes-Guerrero, Mario Daniel", "Galvan-Tejada, Carlos E."),
        }
        defaults.update(overrides)
        return RawArticle(**defaults)

    def test_pid_from_resource_url_with_collection(self) -> None:
        pid = SciELOConnector._scielo_pid_from_url(
            "https://search.scielo.org/resource/pt/S2960-24672026000102026-ven",
        )
        assert pid == ("S2960-24672026000102026", "ven")

    def test_pid_from_resource_url_without_collection(self) -> None:
        pid = SciELOConnector._scielo_pid_from_url(
            "https://search.scielo.org/resource/en/S0100879X1998000800011",
        )
        assert pid == ("S0100879X1998000800011", None)

    def test_pid_from_oai_query_param(self) -> None:
        pid = SciELOConnector._scielo_pid_from_url(
            "https://www.scielo.br/scielo.php?script=sci_arttext"
            "&pid=S0100-879X1998000800011&lng=en",
        )
        # ISSN hyphen is preserved; no collection in the pid= param.
        assert pid == ("S0100-879X1998000800011", None)

    def test_pid_missing_returns_none(self) -> None:
        assert (
            SciELOConnector._scielo_pid_from_url("https://www.scielo.br/a/acupuntura")
            is None
        )
        assert SciELOConnector._scielo_pid_from_url("") is None

    def test_enrich_fills_journal_doi_year_abstract_authors(self, monkeypatch) -> None:
        conn = SciELOConnector()
        monkeypatch.setattr(
            conn,
            "_fetch_articlemeta",
            lambda _c, _coll: self._ARTICLEMETA,
        )

        # The override must NOT call the article-page fetch (the 502 source).
        def _no_page_fetch(*_args, **_kwargs):
            msg = "enrich_raw must not fetch the article landing page"
            raise AssertionError(msg)

        monkeypatch.setattr(conn, "_request_text", _no_page_fetch)

        raw = conn.enrich_raw(self._raw())
        assert raw.journal == "e-Revista Multidisciplinaria del Saber"
        assert raw.doi == "10.61286/E-RMS.V4I.381"
        assert raw.year == 2026
        # English abstract is preferred over the Spanish one.
        assert raw.abstract.startswith("Introduction. Study on diabetes.")
        assert "Resumen" not in raw.abstract
        assert raw.authors == (
            "Cervantes-Guerrero, Mario Daniel",
            "Galvan-Tejada, Carlos E.",
        )

    def test_enrich_strips_abstract_label(self, monkeypatch) -> None:
        conn = SciELOConnector()
        monkeypatch.setattr(
            conn,
            "_fetch_articlemeta",
            lambda _c, _coll: self._ARTICLEMETA,
        )
        monkeypatch.setattr(conn, "_request_text", lambda *_a, **_k: "")

        raw = conn.enrich_raw(self._raw())
        assert not raw.abstract.lower().startswith("abstract")
        assert not raw.abstract.lower().startswith("resumen")

    def test_enrich_api_failure_returns_raw_unchanged(self, monkeypatch) -> None:
        conn = SciELOConnector()
        monkeypatch.setattr(conn, "_fetch_articlemeta", lambda _c, _coll: None)

        original = self._raw()
        raw = conn.enrich_raw(original)
        assert raw.journal == "SciELO"
        assert raw.doi == ""
        assert raw.year == 2026
        assert raw.abstract == original.abstract

    def test_enrich_no_pid_returns_raw_unchanged(self, monkeypatch) -> None:
        conn = SciELOConnector()
        # _fetch_articlemeta must never be reached when no PID is found.
        monkeypatch.setattr(
            conn,
            "_fetch_articlemeta",
            lambda *_a, **_k: pytest.fail("must not call ArticleMeta without a PID"),
        )
        original = self._raw(url="https://www.scielo.br/a/no-pid-here")
        raw = conn.enrich_raw(original)
        assert raw is original or raw.journal == "SciELO"

    def test_authors_surname_given(self) -> None:
        conn = SciELOConnector()
        authors = conn._articlemeta_authors(self._ARTICLEMETA)
        assert authors == (
            "Cervantes-Guerrero, Mario Daniel",
            "Galvan-Tejada, Carlos E.",
        )

    def test_authors_drops_empty_and_dedupes(self) -> None:
        conn = SciELOConnector()
        data = {
            "article": {
                "v10": [
                    {"s": "Smith", "n": "J."},
                    {"s": "Smith", "n": "J."},
                    {"s": "", "n": ""},
                    {"s": "Jones"},
                ],
            },
        }
        assert conn._articlemeta_authors(data) == ("Smith, J.", "Jones")

    def test_abstract_prefers_english_then_original(self) -> None:
        conn = SciELOConnector()
        # English present -> English wins.
        assert conn._articlemeta_abstract(self._ARTICLEMETA).startswith(
            "Introduction. Study on diabetes.",
        )

    def test_abstract_falls_back_to_original_language(self, monkeypatch) -> None:
        conn = SciELOConnector()
        data = {
            "article": {
                "v40": [{"_": "pt"}],
                "v83": [
                    {"l": "pt", "a": "Resumo  Texto em portugues."},
                    {"l": "es", "a": "Resumen  Texto en espanol."},
                ],
            },
        }
        assert conn._articlemeta_abstract(data).startswith("Texto em portugues.")

    def test_abstract_falls_back_to_first_when_no_match(self) -> None:
        conn = SciELOConnector()
        data = {
            "article": {
                "v40": [{"_": "fr"}],
                "v83": [{"l": "de", "a": "Zusammenfassung  Deutsche Text."}],
            },
        }
        assert conn._articlemeta_abstract(data).startswith("Deutsche Text.")

    def test_abstract_strips_italian_singular_label(self) -> None:
        conn = SciELOConnector()
        data = {"article": {"v83": [{"l": "it", "a": "Riassunto  Testo italiano."}]}}
        assert conn._articlemeta_abstract(data).startswith("Testo italiano.")

    def test_abstract_strips_spanish_plural_label(self) -> None:
        conn = SciELOConnector()
        data = {"article": {"v83": [{"l": "es", "a": "Resumenes  Estudio."}]}}
        assert conn._articlemeta_abstract(data).startswith("Estudio.")

    def test_doi_null_does_not_become_string_none(self) -> None:
        conn = SciELOConnector()
        assert conn._articlemeta_doi({"doi": None}) == ""
        assert (
            conn._articlemeta_doi(
                {"article": {"v237": [{"_": None}]}},
            )
            == ""
        )

    def test_journal_v100_null_does_not_become_string_none(self) -> None:
        conn = SciELOConnector()
        assert conn._articlemeta_journal({"title": {"v100": [{"_": None}]}}) == ""

    def test_abstract_a_null_does_not_become_string_none(self) -> None:
        conn = SciELOConnector()
        data = {"article": {"v83": [{"l": "es", "a": None}]}}
        assert conn._articlemeta_abstract(data) == ""

    def test_authors_null_surname_keeps_given_only(self) -> None:
        conn = SciELOConnector()
        data = {"article": {"v10": [{"s": None, "n": "Mario"}]}}
        assert conn._articlemeta_authors(data) == ("Mario",)

    def test_enrich_non_dict_api_payload_returns_raw_unchanged(
        self,
        monkeypatch,
    ) -> None:
        raw = self._raw()
        conn = SciELOConnector()
        monkeypatch.setattr(
            conn,
            "_fetch_articlemeta",
            lambda code, coll: ["unexpected", "list"],
        )
        result = conn.enrich_raw(raw)
        assert result is raw


class TestMathNetEnrichParse:
    """MathNet enrich_raw parses the article page via HTML structure.

    MathNet renders the citation head in ``<title>``, the bibliographic line
    (journal/year/volume/issue/pages/DOI) in the first ``<i>``, and labeled
    fields (Abstract/Keywords/Language) as ``<b>Label:</b>`` + sibling text.
    The previous linearized-text regex mismatched this structure and left
    authors/journal/volume/issue/pages empty; these tests pin the structural
    parse against faithful forthcoming and published fixtures.
    """

    _FORTHCOMING_HTML = (
        "<html><head><title>S. O. Speranski, A. V. Grefenshtein, "
        "“On the complexity of first-order logics of probability”, "
        "Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya</title></head>"
        "<body><i>Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya, "
        "Forthcoming paper</i>"
        "<b>Abstract:</b> This article is concerned with Halpern logics.<br>"
        " Keywords follow."
        "<b>Keywords:</b> probability logic, quantification"
        "<b>Language:</b> English"
        "</body></html>"
    )

    _PUBLISHED_HTML = (
        "<html><head><title>M. V. Zhitlukhin, "
        "“Asymptotically optimal strategies in multi-agent market "
        "models”, Uspekhi Mat. Nauk, 81:3(489) (2026), 3\u201350"
        "</title></head>"
        "<body><i>Uspekhi Matematicheskikh Nauk, 2026, Volume 81 , Issue 3(489) , "
        "Pages 3\u201350 DOI: https://doi.org/10.4213/rm10325</i>"
        "<b>Abstract:</b> A survey of results on asymptotically optimal strategies."
        "<b>Keywords:</b> financial mathematics"
        "<b>Language:</b> Russian"
        "</body></html>"
    )

    def test_citation_head_splits_comma_separated_authors(self) -> None:
        authors, title = MathNetConnector._mathnet_citation_head(
            "S. O. Speranski, A. V. Grefenshtein, “On probability”, J",
        )
        assert authors == "S. O. Speranski, A. V. Grefenshtein"
        assert title == "On probability"

    def test_citation_head_returns_empty_when_no_quote(self) -> None:
        assert MathNetConnector._mathnet_citation_head("no quoted title here") == (
            "",
            "",
        )

    def test_italics_meta_published(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self._PUBLISHED_HTML, "lxml")
        journal, volume, issue, pages, year, doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert journal == "Uspekhi Matematicheskikh Nauk"
        assert volume == "81"
        assert issue == "3(489)"
        assert pages == "3\u201350"
        assert year == "2026"
        assert doi == "10.4213/rm10325"

    def test_italics_meta_forthcoming_has_journal_only(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self._FORTHCOMING_HTML, "lxml")
        journal, volume, issue, pages, year, doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert journal == "Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya"
        assert volume == issue == pages == year == doi == ""

    def test_italics_meta_journal_name_with_comma_is_not_truncated(self) -> None:
        from bs4 import BeautifulSoup

        # Journal full name contains a comma; the year marker anchors the
        # split so "Series A" is preserved.
        html = (
            "<html><body><i>Transactions of the Moscow Math. Society, "
            "Series A, 2026, Volume 71, Issue 3, Pages 174\u2013185 "
            "DOI: https://doi.org/10.4213/tmm71</i></body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        journal, volume, issue, pages, year, doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert journal == "Transactions of the Moscow Math. Society, Series A"
        assert volume == "71"
        assert issue == "3"
        assert pages == "174\u2013185"
        assert year == "2026"
        assert doi == "10.4213/tmm71"

    def test_italics_meta_single_page_is_captured(self) -> None:
        from bs4 import BeautifulSoup

        # Errata / short notes carry a lone page, not a range.
        html = (
            "<html><body><i>Short Notes Journal, 2026, Volume 5, Issue 1, "
            "Pages 42 DOI: https://doi.org/10.4213/sn5</i></body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        _journal, _volume, _issue, pages, _year, _doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert pages == "42"

    def test_labeled_value_captures_multiline_abstract(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self._FORTHCOMING_HTML, "lxml")
        abstract = MathNetConnector._mathnet_labeled_value(soup, "Abstract:")
        assert "Halpern logics" in abstract
        assert "Keywords follow" in abstract

    def test_language_code_maps_known_labels(self) -> None:
        assert MathNetConnector._mathnet_language_code("English") == "en"
        assert MathNetConnector._mathnet_language_code("Russian") == "ru"
        assert MathNetConnector._mathnet_language_code("Klingon") == ""

    def test_enrich_raw_forthcoming_fills_authors_journal_abstract(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = MathNetConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._FORTHCOMING_HTML,
        )
        raw = RawArticle(
            source_key="mathnet",
            title="On the complexity of first-order logics of probability",
            url="https://www.mathnet.ru/eng/im9718",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="MathNet.Ru",
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.authors == ("S. O. Speranski", "A. V. Grefenshtein")
        assert (
            enriched.journal
            == "Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya"
        )
        assert "Halpern logics" in enriched.abstract
        assert enriched.language == "en"
        # Forthcoming paper has no volume/issue/pages/year in the <i> line.
        assert enriched.volume == enriched.issue == enriched.pages == ""

    def test_enrich_raw_published_fills_biblio_and_doi(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = MathNetConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._PUBLISHED_HTML,
        )
        raw = RawArticle(
            source_key="mathnet",
            title="Asymptotically optimal strategies in multi-agent market models",
            url="https://www.mathnet.ru/eng/rm10325",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="MathNet.Ru",
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.authors == ("M. V. Zhitlukhin",)
        assert (
            enriched.title
            == "Asymptotically optimal strategies in multi-agent market models"
        )
        assert enriched.journal == "Uspekhi Matematicheskikh Nauk"
        assert enriched.volume == "81"
        assert enriched.issue == "3(489)"
        assert enriched.pages == "3\u201350"
        assert enriched.year == 2026
        assert enriched.doi == "10.4213/rm10325"
        assert enriched.language == "ru"
        assert "asymptotically optimal strategies" in enriched.abstract

    def test_enrich_raw_keeps_existing_authors_when_title_has_no_quote(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = MathNetConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: "<html></html>",
        )
        raw = RawArticle(
            source_key="mathnet",
            title="t",
            url="https://www.mathnet.ru/eng/x1",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="MathNet.Ru",
            authors=("Existing Author",),
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.authors == ("Existing Author",)


class TestAJOLAbstractFromArticlePage:
    """AJOL ``enrich_raw`` replaces page-range OAI abstracts with the real one.

    AJOL OAI ``dc:description`` is occasionally just the article page span
    (e.g. ``"8-16"``). The article page exposes the true abstract in
    ``div.article-abstract``; ``enrich_raw`` must prefer it over the
    page-range pseudo-abstract, while keeping a legitimate OAI abstract
    when no article-page abstract is present.
    """

    _ARTICLE_HTML = (
        "<html><head>"
        '<meta name="citation_journal_title" content="West African Journal"/>'
        '<meta name="citation_date" content="2017/09/20"/>'
        "</head><body>"
        "<h2>Abstract</h2>"
        '<div class="article-abstract"><p>'
        "The two major motivations in medical science are to prevent and "
        "diagnose diseases with care."
        "</p></div>"
        "</body></html>"
    )

    _ARTICLE_HTML_NO_ABSTRACT = (
        "<html><head>"
        '<meta name="citation_journal_title" content="West African Journal"/>'
        "</head><body><p>no abstract here</p></body></html>"
    )

    # Heading INSIDE the abstract container — the extractor must prefer the
    # ``<p>`` over the bare ``div`` so the literal word "Abstract" does not
    # leak into the extracted abstract (``select_one`` returns matches in
    # document order, so a bare ``div.article-abstract`` listed alongside the
    # ``<p>`` selector would always win and include the heading text).
    _ARTICLE_HTML_HEADING_INSIDE = (
        "<html><body>"
        '<div class="article-abstract">'
        "<h3>Abstract</h3>"
        "<p>The two major motivations in medical science are to prevent and "
        "diagnose diseases with care.</p>"
        "</div>"
        "</body></html>"
    )

    def test_page_range_abstract_replaced_by_article_page_abstract(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = AJOLConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML,
        )
        raw = RawArticle(
            source_key="ajol",
            title="A framework for diagnosing confusable diseases",
            url="https://www.ajol.info/index.php/wajiar/article/view/161476",
            abstract="8-16",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="AJOL",
        )
        enriched = conn.enrich_raw(raw)
        assert "diagnose diseases with care" in enriched.abstract
        assert enriched.abstract != "8-16"

    def test_legitimate_oai_abstract_kept_when_no_article_page_abstract(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = AJOLConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_NO_ABSTRACT,
        )
        raw = RawArticle(
            source_key="ajol",
            title="Some real article",
            url="https://www.ajol.info/index.php/wajiar/article/view/1",
            abstract="A genuine long abstract describing the study in detail.",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="AJOL",
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.abstract == (
            "A genuine long abstract describing the study in detail."
        )

    def test_heading_inside_abstract_div_does_not_leak(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = AJOLConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_HEADING_INSIDE,
        )
        raw = RawArticle(
            source_key="ajol",
            title="Heading inside abstract div",
            url="https://www.ajol.info/index.php/wajiar/article/view/2",
            abstract="8-16",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="AJOL",
        )
        enriched = conn.enrich_raw(raw)
        assert "diagnose diseases with care" in enriched.abstract
        # The literal heading word must not be prepended to the abstract.
        assert not enriched.abstract.lower().startswith("abstract")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("8-16", True),
            ("37-53", True),
            (" 8 – 16 ", True),  # noqa: RUF001 - en dash is the test payload
            ("8–16", True),  # noqa: RUF001 - en dash is the test payload
            ("A real abstract sentence.", False),
            ("", False),
            ("pages 8-16 and more", False),
            ("1234567890123", False),
        ],
    )
    def test_looks_like_page_range(self, text: str, expected: bool) -> None:
        assert AJOLConnector._looks_like_page_range(text) is expected


class TestChallengePageDetector:
    """Regression guard for the residual challenge-page detector.

    The detector must catch real Cloudflare/BunnyCDN interstitials while
    leaving legitimate pages that merely reference Cloudflare (a
    ``cdnjs.cloudflare.com`` CDN script URL — the CiNii false positive — or a
    ``Ray ID`` footer) or discuss challenges in prose (a ``proof-of-work``
    abstract) untouched.
    """

    @pytest.mark.parametrize(
        ("html", "reason"),
        [
            (
                "<html><head><title>Just a moment...</title></head>"
                '<body><div id="challenge-running"></div>'
                '<div class="cf-browser-verification"></div></body></html>',
                "cf challenge interstitial",
            ),
            (
                '<html><body><script src="/cdn-cgi/challenge-platform/h/'
                'g/orchestrate/managed/v1"></script></body></html>',
                "cdn-cgi challenge-platform path",
            ),
            (
                "<html><body><script>window._cf_chl_opt={}</script></body></html>",
                "_cf_chl_opt js var",
            ),
            (
                '<html><body><div class="cf-turnstile"'
                ' data-sitekey="x"></div></body></html>',
                "turnstile widget",
            ),
            (
                "<html><head><title>Attention Required! | Cloudflare</title>"
                "</head><body>Sorry, you have been blocked.</body></html>",
                "cf block page",
            ),
        ],
    )
    def test_detects_real_challenge_pages(self, html: str, reason: str) -> None:
        assert BaseConnector._looks_like_challenge_page(html) is True, reason

    @pytest.mark.parametrize(
        ("html", "reason"),
        [
            # The CiNii false positive: a legitimate page loading jsrender from
            # the cdnjs.cloudflare.com CDN. The bare word "cloudflare" must not
            # trip the detector.
            (
                "<html><body>Back to top</body>"
                '<script src="https://cdnjs.cloudflare.com/ajax/libs/'
                'jsrender/1.0.7/jsrender.min.js"></script></html>',
                "cdnjs CDN script url",
            ),
            # A normal Cloudflare-proxied page footer with a Ray ID — not a
            # challenge page.
            (
                "<html><body>Article body</body>"
                "<footer>Ray ID: 8a4f-abc123</footer></html>",
                "ray id footer",
            ),
            # A scholarly abstract that discusses proof-of-work consensus.
            (
                "<html><body><p>This paper analyses the proof-of-work scheme"
                " used in early cryptocurrencies.</p></body></html>",
                "proof-of-work in abstract prose",
            ),
            # Generic phrases that previously caused false positives.
            (
                "<html><body><noscript>Please enable JavaScript to use this"
                " site.</noscript></body></html>",
                "enable javascript noscript",
            ),
            (
                "<html><body>Please wait while the dataset loads.</body></html>",
                "please wait loader",
            ),
            (
                "<html><body>Security check completed for this session.</body></html>",
                "security check prose",
            ),
            ("", "empty body"),
        ],
    )
    def test_does_not_flag_legitimate_pages(
        self,
        html: str,
        reason: str,
    ) -> None:
        assert BaseConnector._looks_like_challenge_page(html) is False, reason
