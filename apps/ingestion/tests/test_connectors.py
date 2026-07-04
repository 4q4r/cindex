# ruff: noqa: RUF001
from bs4 import BeautifulSoup

from apps.ingestion.connectors import (
    AJOLConnector,
    BaseConnector,
    DOAJConnector,
    EuropePMCConnector,
    JSTAGEConnector,
    MathNetConnector,
    OpenEditionConnector,
    PerseeConnector,
    RawArticle,
    SourceProfile,
)


class _HtmlResponse:
    """Dummy HTML response for parser sanitation tests."""

    def __init__(self, content: bytes, content_type: str) -> None:
        self.content = content
        self.headers = {"Content-Type": content_type}


class _ScriptConnector(BaseConnector):
    """Dummy connector whose landing page contains boilerplate scripts."""

    profile = SourceProfile(
        source_key="script",
        search_url="https://example.org/search",
    )

    def _request_response(self, url: str, params=None, accept=None):  # type: ignore[override]
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
        response = _HtmlResponse(
            content=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
        return self._build_scraper(), response, html

    def _extract_pdf_url(self, soup, *blobs):  # type: ignore[override]
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


def test_jstage_html_extraction_detects_doi_and_year() -> None:
    """Test jstage html extraction detects doi and year helper."""
    html = """
    <html><body>
      <article class='search-result-item'>
        <h3><a href='/article/123'>Deep Study 2024 DOI 10.1234/abcd.5678</a></h3>
        <p class='abstract'>Peer reviewed publication in major index.</p>
        <span class='journal'>Journal of Evidence</span>
      </article>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    items = JSTAGEConnector()._extract_from_html("deep study", soup, limit=5)

    assert len(items) == 1
    assert items[0].doi == "10.1234/abcd.5678"
    assert items[0].year == 2024
    assert items[0].journal == "Journal of Evidence"


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
                        "fullTextUrl": [{"url": "https://example.org/full"}]
                    },
                }
            ]
        }
    }
    items = EuropePMCConnector()._extract_from_payload(
        "systems biology", payload, limit=5
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
                }
            }
        ]
    }
    items = DOAJConnector()._extract_from_payload("ecology", payload, limit=5)

    assert len(items) == 1
    assert items[0].source_key == "doaj"
    assert items[0].journal == "Ecology OA"


def test_ajol_oa_filter_keeps_only_open_access() -> None:
    """Test ajol oa filter keeps only open access helper."""
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
    <html><body>
      <div>A. N. Yakusheva, “Nontransitivity in Trybula triplets:</div>
      <div>stability under sums and maxima transformations”,</div>
      <div>Teor. Veroyatnost. i Primenen., 71:1 (2026), 174–185</div>
      <div>DOI: https://doi.org/10.4213/tvp5882</div>
      <div>
        Abstract: This paper investigates two nontransitive triplets originally
        proposed by S.Trybula.
      </div>
      <div>Keywords: nontransitive triplets</div>
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
        xml, "history france", remaining=5
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


def test_persee_oai_parser_keeps_real_articles() -> None:
    """Test persee oai parser keeps real articles."""
    xml = """
    <oai:OAI-PMH xmlns:oai="http://www.openarchives.org/OAI/2.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <oai:ListRecords>
        <oai:record>
          <oai:metadata>
            <dc:title>Sociologie et neurosciences</dc:title>
            <dc:description>Peer reviewed sociology article</dc:description>
            <dc:identifier>https://www.persee.fr/doc/example_2026_1</dc:identifier>
            <dc:identifier>https://doi.org/10.4000/example.2026.1</dc:identifier>
            <dc:source>Revue de sociologie</dc:source>
          </oai:metadata>
        </oai:record>
      </oai:ListRecords>
    </oai:OAI-PMH>
    """
    items, token = PerseeConnector()._parse_oai_records(xml, "sociologie", 5)

    assert token == ""
    assert len(items) == 1
    assert items[0].doi == "10.4000/example.2026.1"
