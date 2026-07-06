# ruff: noqa: RUF001
import pytest
from bs4 import BeautifulSoup

from apps.ingestion.connectors import (
    AJOLConnector,
    BaseConnector,
    CiNiiConnector,
    DOAJConnector,
    EuropePMCConnector,
    IACRConnector,
    MathNetConnector,
    OpenEditionConnector,
    PerseeConnector,
    PMCConnector,
    RawArticle,
    SciEngineConnector,
    SourceProfile,
)


class _ScriptConnector(BaseConnector):
    """Dummy connector whose landing page contains boilerplate scripts."""

    profile = SourceProfile(
        source_key="script",
        search_url="https://example.org/search",
    )

    def _request_text(self, url, params=None, *, ocr_language="eng") -> str:  # type: ignore[override]
        html = """
        <html>
          <head>
            <title>Scripted Article</title>
            <script>window.__metric = "ym(70895752, 'init')";</script>
          </head>
          <body>
            <article>
              <h1>Scripted Article on Neurobiology</h1>
              <p>Real article text about neurobiology and AI.</p>
            </article>
          </body>
        </html>
        """
        return html

    def _extract_pdf_url(self, soup, *blobs) -> str:  # type: ignore[override]
        return ""


def test_html_enrichment_ignores_script_boilerplate() -> None:
    """HTML enrichment should not leak script text into article content."""
    connector = _ScriptConnector()
    raw = RawArticle(
        source_key="script",
        title="Scripted Article on Neurobiology",
        url="https://example.org/article",
        abstract="",
        full_text="",
        language="en",
        year=2024,
        doi="10.1234/script.1",
        journal="Script Journal",
    )

    enriched = connector.enrich_raw(raw)

    assert "Real article text about neurobiology and AI." in enriched.full_text
    assert "ym(70895752" not in enriched.full_text


def test_europe_pmc_payload_extraction() -> None:
    """Test europe pmc payload extraction helper."""
    payload = {
        "resultList": {
            "result": [
                {
                    "title": "Systems Biology in 2023",
                    "abstractText": "A journal article with DOI",
                    "journalTitle": "Bio Journal",
                    "doi": "10.1111/sysbio.2023.1",
                    "pubYear": "2023",
                    "fullTextUrlList": {
                        "fullTextUrl": [{"url": "https://example.org/full"}],
                    },
                },
            ],
        },
    }
    items = EuropePMCConnector()._extract_from_payload(
        "systems biology",
        payload,
        limit=5,
    )

    assert len(items) == 1
    assert items[0].source_key == "europe_pmc"
    assert items[0].doi == "10.1111/sysbio.2023.1"
    assert items[0].year == 2023


def test_doaj_payload_extraction() -> None:
    """Test doaj payload extraction helper."""
    payload = {
        "results": [
            {
                "bibjson": {
                    "title": "Open Access Ecology",
                    "abstract": "Peer-reviewed ecology article",
                    "year": 2022,
                    "journal": {"title": "Ecology OA"},
                    "identifier": [{"type": "doi", "id": "10.2222/ecology.2022.9"}],
                    "link": [{"url": "https://example.org/oa-ecology"}],
                },
            },
        ],
    }
    items = DOAJConnector()._extract_from_payload("ecology", payload, limit=5)

    assert len(items) == 1
    assert items[0].source_key == "doaj"
    assert items[0].journal == "Ecology OA"


@pytest.mark.parametrize(
    ("raw_abstract", "expected"),
    [
        # BMC / BioData Central structured abstracts prepend a literal
        # "Abstract" heading followed by a capitalised section header.
        (
            "Abstract Background Constructing a predictive model is challenging",
            "Background Constructing a predictive model is challenging",
        ),
        (
            "Abstract: Background Constructing a predictive model",
            "Background Constructing a predictive model",
        ),
        (
            "ABSTRACT The study examines the role of X.",
            "The study examines the role of X.",
        ),
        # A colon label is unambiguous regardless of the next token's case.
        (
            "Abstract: this article reviews the literature.",
            "this article reviews the literature.",
        ),
        # Full-width colon (CJK punctuation) is also a label.
        (
            "Abstract：Background Constructing a predictive model",
            "Background Constructing a predictive model",
        ),
        # A newline between label and body is still a label.
        (
            "Abstract\nBackground Constructing a predictive model",
            "Background Constructing a predictive model",
        ),
        # Capitalised next token is treated as a label (structured-abstract
        # section header or sentence start), even when the word could be a body
        # term if it were lower-cased.
        (
            "Abstract Algebra is central to modern mathematics.",
            "Algebra is central to modern mathematics.",
        ),
        # Legitimate sentences that merely start with the word "Abstract" must
        # be preserved — the next token is lower-case and there is no colon.
        (
            "Abstract algebra is a branch of mathematics.",
            "Abstract algebra is a branch of mathematics.",
        ),
        # No leading label at all — untouched.
        ("Peer-reviewed ecology article", "Peer-reviewed ecology article"),
        ("", ""),
    ],
)
def test_doaj_strip_abstract_label(raw_abstract: str, expected: str) -> None:
    """Leading ``Abstract`` label is stripped only when it is a label.

    Publishers such as BMC / BioData Central emit the abstract field with a
    literal ``"Abstract"`` heading (``"Abstract Background ..."``); the helper
    removes that heading but preserves real sentences that happen to start
    with the word ``Abstract`` (e.g. ``"Abstract algebra is..."``).
    """
    assert DOAJConnector()._strip_abstract_label(raw_abstract) == expected


def test_doaj_payload_strips_leading_abstract_label() -> None:
    """End-to-end: a DOAJ record whose abstract starts with ``Abstract`` is
    extracted with the label stripped from both ``abstract`` and
    ``full_text``."""
    payload = {
        "results": [
            {
                "bibjson": {
                    "title": "Preeclampsia prediction pipeline",
                    "abstract": (
                        "Abstract Background Constructing a predictive model"
                        " is challenging in imbalanced medical data."
                    ),
                    "year": 2025,
                    "journal": {"title": "BioData Mining"},
                    "identifier": [
                        {"type": "doi", "id": "10.1186/s13040-025-00440-1"},
                    ],
                    "link": [{"url": "https://doi.org/10.1186/s13040-025-00440-1"}],
                },
            },
        ],
    }
    items = DOAJConnector()._extract_from_payload("preeclampsia", payload, limit=5)

    assert len(items) == 1
    assert items[0].abstract.startswith("Background Constructing")
    assert "Abstract " not in items[0].abstract
    assert items[0].full_text.startswith("Preeclampsia prediction pipeline")
    # The label must not leak into full_text either.
    assert "Abstract Background" not in items[0].full_text


def test_cinii_payload_extraction_prefers_real_year_and_doi() -> None:
    """Test cinii payload extraction prefers real year and doi helper.

    The CiNii OpenSearch ``@id``/``link`` CRID is a long numeric identifier
    whose first four digits (``1970`` here) are not a publication year, and
    the DOI lives in a typed ``dc:identifier`` entry, so the parser must read
    the year from ``prism:publicationDate`` and the DOI from the
    ``cir:DOI`` identifier rather than scanning the raw JSON dump.
    """
    payload = {
        "items": [
            {
                "@id": "https://cir.nii.ac.jp/crid/1970012345678901234",
                "title": "CiNii Live Hook Article 2024",
                "link": {"@id": "https://cir.nii.ac.jp/crid/1970012345678901234"},
                "dc:creator": ["Sato, Hanako", "Tanaka, Ichiro"],
                "prism:publicationName": "CiNii Journal",
                "prism:publicationDate": "2024-05-01",
                "description": "Peer reviewed article with DOI 10.1234/cinii.2024.1",
                "dc:identifier": [
                    {"@type": "cir:NAID", "@value": "10000000000"},
                    {"@type": "cir:DOI", "@value": "10.1234/cinii.2024.1"},
                ],
                "dc:source": [{"@id": "https://ci.nii.ac.jp/ncid/AA00000000"}],
            },
        ],
    }
    items = CiNiiConnector()._extract_from_payload("machine learning", payload, limit=3)

    assert len(items) == 1
    assert items[0].year == 2024
    assert items[0].doi == "10.1234/cinii.2024.1"
    assert items[0].journal == "CiNii Journal"
    assert items[0].authors == ("Sato, Hanako", "Tanaka, Ichiro")
    assert items[0].url == "https://cir.nii.ac.jp/crid/1970012345678901234"
    # Latin-only title -> language inferred as empty (unknown), not the old
    # hardcoded "ja" profile default.
    assert items[0].language == ""


def test_cinii_book_without_doi_keeps_clean_journal() -> None:
    """Test cinii book without doi keeps clean journal helper.

    A book item has no ``prism:publicationName`` and a dict-shaped
    ``dc:source`` (a URI ``@id``), so the journal must fall back to
    ``dc:publisher`` rather than render the dict as garbage, and the DOI
    must stay empty instead of leaking a URI fragment.
    """
    payload = {
        "items": [
            {
                "@id": "https://cir.nii.ac.jp/crid/1970098765432109876",
                "title": "Reinforcement and systemic machine learning",
                "link": {"@id": "https://cir.nii.ac.jp/crid/1970098765432109876"},
                "dc:creator": "Kulkarni, Parag",
                "dc:publisher": "IEEE Press",
                "prism:publicationDate": "2012",
                "description": "IEEE Press series on systems science and engineering",
                "dc:identifier": [
                    {"@type": "cir:NCID", "@value": "BB11467443"},
                    {"@type": "cir:ISBN", "@value": "9780470919996"},
                ],
                "dc:source": [{"@id": "https://ci.nii.ac.jp/ncid/BB11467443"}],
            },
        ],
    }
    items = CiNiiConnector()._extract_from_payload("machine learning", payload, limit=3)

    assert len(items) == 1
    assert items[0].year == 2012
    assert items[0].doi == ""
    assert items[0].journal == "IEEE Press"
    assert items[0].authors == ("Kulkarni, Parag",)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Latin-only English proceedings -> unknown, not the old hardcoded "ja".
        ("31st International Conference on Machine Learning", ""),
        ("Reinforcement and systemic machine learning for decision making", ""),
        # Hiragana -> Japanese.
        ("しくみがわかる深層学習", "ja"),
        # Katakana -> Japanese.
        ("Python パイソン 深層学習入門", "ja"),
        # Pure Han ideographs, no kana -> Japanese for the CiNii context.
        ("深層学習入門", "ja"),
        # Halfwidth Katakana -> Japanese.
        ("ｱｲｳｴｵ", "ja"),
        # Empty / whitespace -> unknown.
        ("", ""),
        ("   ", ""),
    ],
)
def test_cinii_infer_language_from_script(text: str, expected: str) -> None:
    """CiNii language is inferred from Unicode script, not a hardcoded ja."""
    assert CiNiiConnector._infer_cinii_language(text) == expected


def test_ajol_oa_filter_keeps_only_open_access(monkeypatch) -> None:
    """Test ajol oa filter keeps only open access helper.

    ``enrich_raw`` fetches the article page over the network; the OA
    filter logic under test does not depend on enrichment, so patch it
    to a passthrough and keep the test offline.
    """
    monkeypatch.setattr(AJOLConnector, "enrich_raw", lambda self, raw: raw)
    html = """
    <html><body>
      <article>
        <h3><a href='/a1'>Clinical Methods 2021</a></h3>
        <p class='abstract'>Open Access full text available, DOI 10.3000/ajol.1</p>
      </article>
      <article>
        <h3><a href='/a2'>Closed Article 2022</a></h3>
        <p class='abstract'>Subscription content only, DOI 10.3000/ajol.2</p>
      </article>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    items = AJOLConnector()._extract_from_html("clinical", soup, limit=10)

    assert len(items) == 1
    assert "10.3000/ajol.1" in items[0].doi


def test_mathnet_enrichment_extracts_bibliographic_fields(monkeypatch) -> None:
    """Test mathnet enrichment extracts bibliographic fields helper."""
    html = """
    <html><head>
      <title>A. N. Yakusheva, "Nontransitivity in Trybula triplets:
      stability under sums and maxima transformations",
      Teor. Veroyatnost. i Primenen.</title>
    </head><body>
      <i>Teor. Veroyatnost. i Primenen., 2026, Volume 71, Issue 1,
      Pages 174–185 DOI: https://doi.org/10.4213/tvp5882</i>
      <p><b>Abstract:</b> This paper investigates two nontransitive triplets
      originally proposed by S.Trybula.</p>
      <p><b>Keywords:</b> nontransitive triplets</p>
    </body></html>
    """
    connector = MathNetConnector()
    monkeypatch.setattr(connector, "_request_text", lambda *args, **kwargs: html)
    raw = connector._raw(
        title=(
            "Nontransitivity in Trybula triplets: stability under sums and "
            "maxima transformations"
        ),
        url=(
            "https://www.mathnet.ru/php/archive.phtml?wshow=paper&jrnid=tvp"
            "&paperid=5882&option_lang=eng"
        ),
        abstract="",
        full_text="",
        doi="",
        year=None,
        journal="MathNet.Ru",
    )

    enriched = connector.enrich_raw(raw)

    assert enriched.doi == "10.4213/tvp5882"
    assert enriched.journal == "Teor. Veroyatnost. i Primenen."
    assert enriched.volume == "71"
    assert enriched.issue == "1"
    assert enriched.pages == "174–185"
    assert enriched.authors == ("A. N. Yakusheva",)
    assert "nontransitive triplets" in enriched.abstract.lower()


def test_openedition_filters_hypotheses_blog_posts() -> None:
    """Test openedition filters hypotheses blog posts helper."""
    xml = """
    <oai:OAI-PMH xmlns:oai="http://www.openarchives.org/OAI/2.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <oai:ListRecords>
        <oai:record>
          <oai:metadata>
            <dc:title>History article on France</dc:title>
            <dc:description>Peer reviewed history article</dc:description>
            <dc:identifier>https://doi.org/10.4000/example.2026.1</dc:identifier>
            <dc:identifier>https://journals.openedition.org/example/1234</dc:identifier>
            <dc:source>OpenEdition Journal</dc:source>
          </oai:metadata>
        </oai:record>
        <oai:record>
          <oai:metadata>
            <dc:title>Le Cid à l'honneur</dc:title>
            <dc:description>Blog post about theatre</dc:description>
            <dc:identifier>https://doi.org/10.58079/164u7</dc:identifier>
            <dc:identifier>https://corneille.hypotheses.org/3002</dc:identifier>
            <dc:source>URI:https://corneille.hypotheses.org</dc:source>
          </oai:metadata>
        </oai:record>
      </oai:ListRecords>
    </oai:OAI-PMH>
    """
    items, token = OpenEditionConnector()._parse_oai_records(
        xml,
        "history france",
        remaining=5,
    )

    assert token == ""
    assert len(items) == 1
    assert items[0].doi == "10.4000/example.2026.1"
    assert "hypotheses.org" not in items[0].url


def test_base_html_extraction_does_not_emit_stub_links() -> None:
    """Test base html extraction does not emit stub links helper."""
    html = """
    <html><body>
      <a href="/article/1">Psychology and Neuroscience : Towards a Common Language</a>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    items = PerseeConnector()._extract_from_html("AI in neuroscience", soup, limit=5)

    assert items == []


def test_persee_html_parser_keeps_real_articles() -> None:
    """Test persee html parser keeps real articles helper.

    The Persee keyword HTML search wraps each hit in a ``.doc-result``
    card with ``a.title`` (carrying a ``?q=`` suffix to strip), per-author
    ``.contributors .name`` spans, the journal in
    ``.documentBibRef .collection a``, the year in ``.documentYear`` and a
    relevance-highlighted abstract in ``.searchContext``.
    """
    html = """
    <html><body>
      <article class='doc-result'>
        <a class='title' href='https://www.persee.fr/doc/aso_2018_1?q=sociologie'>
          Econometrics and Machine Learning
        </a>
        <div class='contributors'>
          <span class='name'>Arthur Charpentier</span>
          <span class='name'>Emmanuel Flachaire</span>
        </div>
        <div class='documentBibRef'><span class='collection'>
          <a href='/journal/aso'>Economie et Statistique</a>
        </span></div>
        <div class='documentYear'>Année 2018</div>
        <div class='searchContext'>Advances in <em>machine learning</em>.</div>
      </article>
      <article class='doc-result'>
        <a class='title' href='https://www.persee.fr/doc/example_2026_1?q=sociologie'>
          Sociologie et neurosciences DOI 10.4000/example.2026.1
        </a>
        <div class='contributors'><span class='name'>Jean Dupont</span></div>
        <div class='documentBibRef'><span class='collection'>
          <a href='/journal/rs'>Revue de sociologie</a>
        </span></div>
        <div class='documentYear'>Année 2026</div>
        <div class='searchContext'>Peer reviewed sociology article.</div>
      </article>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    items = PerseeConnector()._extract_from_html("sociologie", soup, limit=5)

    assert len(items) == 2
    first = items[0]
    assert first.title == "Econometrics and Machine Learning"
    assert first.url == "https://www.persee.fr/doc/aso_2018_1"
    assert "?q=" not in first.url
    assert first.journal == "Economie et Statistique"
    assert first.year == 2018
    assert first.authors == ("Arthur Charpentier", "Emmanuel Flachaire")
    assert "machine learning" in first.abstract.lower()
    assert items[1].doi == "10.4000/example.2026.1"
    assert items[1].year == 2026


def test_sciengine_payload_extraction_rebuilds_doi_url_and_strips_html() -> None:
    """Test sciengine payload extraction rebuilds doi url and strips html helper.

    The SciEngine ``/SciSearch/searchNew`` endpoint returns ``relateList``
    items that carry a DOI but no article URL and no journal, with the
    abstract delivered as an HTML fragment (``intro_en``/``intro_cn``) and
    authors as a ``fullname_en``/``fullname_cn`` list. The parser must
    rebuild the URL from the DOI, strip the HTML from the abstract, read the
    year from ``pubYear`` and report ``SciEngine`` as the journal rather than
    leaking nav-menu garbage.
    """
    payload = {
        "relateList": [
            {
                "id": "202211010",
                "doi": "10.12016/j.issn.2096-1456.2022.11.010",
                "pubYear": "2022",
                "pubDateStr": "Nov 20, 2022",
                "title_en": (
                    "Development of artificial intelligence application in "
                    "oral clinical diagnosis and treatment"
                ),
                "title_cn": "",
                "fullname_en": ["Chang LI", "Cui HUANG", "Hongye YANG"],
                "fullname_cn": [],
                "intro_en": (
                    '<p id="p00015">With the arrival of the era of big data, '
                    "artificial intelligence has developed rapidly.</p>"
                ),
                "intro_cn": "",
            },
        ],
    }
    items = SciEngineConnector()._extract_from_payload(
        "artificial intelligence",
        payload,
        limit=3,
    )

    assert len(items) == 1
    first = items[0]
    assert first.title.startswith("Development of artificial intelligence")
    assert first.url == "https://doi.org/10.12016/j.issn.2096-1456.2022.11.010"
    assert first.doi == "10.12016/j.issn.2096-1456.2022.11.010"
    assert first.year == 2022
    assert first.journal == "SciEngine"
    assert first.authors == ("Chang LI", "Cui HUANG", "Hongye YANG")
    assert "<p" not in first.abstract
    assert "big data" in first.abstract.lower()


def test_pmc_strips_conference_abstract_number_and_falls_back_to_pub_date() -> None:
    """Test pmc strips conference abstract number helper.

    Europe PMC concatenates the poster/abstract number into ``title`` for
    conference-supplement records (``pubType`` ``['Abstract']``), producing
    ``"122 Statistically valid ..."``. The parser must strip the leading
    number for those records only. The same records omit ``pubYear`` while
    carrying ``firstPublicationDate`` (``YYYY-MM-DD``), so the year must
    fall back to that date instead of leaking ``None``.
    """
    payload = {
        "resultList": {
            "result": [
                {
                    "title": "122 Statistically valid fairness evaluation",
                    "abstractText": "",
                    "pmcid": "PMC13173230",
                    "pubTypeList": {"pubType": ["Abstract"]},
                    "firstPublicationDate": "2026-05-20",
                    "authorString": "Malik M, Watson D, Beenken M",
                    "journalInfo": {
                        "journal": {
                            "title": "Journal of clinical and translational science",
                        },
                    },
                },
                {
                    "title": "5G networks for biomedical telemetry",
                    "abstractText": "A regular article that starts with a number.",
                    "doi": "10.1111/pmc.regular.2024",
                    "pubYear": "2024",
                    "pmcid": "PMC9999999",
                    "pubTypeList": {"pubType": ["Journal Article"]},
                },
            ],
        },
    }
    items = PMCConnector()._extract_from_payload("machine learning", payload, limit=5)

    assert len(items) == 2
    conf = items[0]
    assert conf.title == "Statistically valid fairness evaluation"
    assert conf.year == 2026
    assert conf.url == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC13173230/"
    assert conf.authors == ("Malik M", "Watson D", "Beenken M")
    regular = items[1]
    assert regular.title == "5G networks for biomedical telemetry"
    assert regular.year == 2024
    assert regular.doi == "10.1111/pmc.regular.2024"


def test_iacr_rss_filter_keeps_matching_eprints_and_skips_off_topic() -> None:
    """Test iacr rss filter keeps matching eprints and skips off topic helper.

    The IACR ePrint search page is blocked by an anti-bot "tin foil hat"
    wall and no public search API exists, so the connector reads the RSS
    feed and filters client-side: only records whose title or abstract
    contains every query token are kept, off-topic records are dropped.
    IACR does not assign DOIs, so ``doi`` must stay empty rather than
    fabricated, and the year is read from the RSS ``pubDate``.
    """
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0' xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        "<channel>"
        "<item>"
        "<title>Federated machine learning with secure enclaves</title>"
        "<link>https://eprint.iacr.org/2024/123</link>"
        "<description>Privacy-preserving machine learning training.</description>"
        "<pubDate>Mon, 15 Jan 2024 00:00:00 +0000</pubDate>"
        "<dc:creator>Alice Smith</dc:creator>"
        "<dc:creator>Bob Jones</dc:creator>"
        "</item>"
        "<item>"
        "<title>Hash function collisions revisited</title>"
        "<link>https://eprint.iacr.org/2023/456</link>"
        "<description>Bounds on collision resistance.</description>"
        "<pubDate>Wed, 01 Feb 2023 00:00:00 +0000</pubDate>"
        "<dc:creator>Carol Lee</dc:creator>"
        "</item>"
        "</channel></rss>"
    )
    connector = IACRConnector()
    records = connector._parse_rss_xml(xml)
    items = connector._build_articles("machine learning", records, limit=5)

    assert len(items) == 1
    only = items[0]
    assert only.source_key == "iacr"
    assert only.title == "Federated machine learning with secure enclaves"
    assert only.url == "https://eprint.iacr.org/2024/123"
    assert only.year == 2024
    assert only.doi == ""
    assert only.authors == ("Alice Smith", "Bob Jones")
    assert only.journal == "IACR ePrint"
    assert "machine learning" in only.abstract.lower()
