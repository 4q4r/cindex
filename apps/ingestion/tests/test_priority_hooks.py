from bs4 import BeautifulSoup

from apps.ingestion.connectors import CiNiiConnector, COREConnector


def test_cinii_payload_hook_parses_opensearch_items() -> None:
    """Test cinii payload hook parses opensearch items helper."""
    payload = {
        "items": [
            {
                "title": "CiNii Hook 2025",
                "link": {"@id": "https://cir.nii.ac.jp/crid/abc"},
                "description": "Entry DOI 10.7777/cinii.hook",
                "dc:date": "2025-03-10",
                "prism:publicationName": "CiNii Test Journal",
            }
        ]
    }
    items = CiNiiConnector()._extract_from_payload("machine learning", payload, limit=3)

    assert len(items) == 1
    assert items[0].doi == "10.7777/cinii.hook"
    assert items[0].journal == "CiNii Test Journal"


def test_core_hook_reads_next_data_payload() -> None:
    """Test core hook reads next data payload helper."""
    html = """
    <html><head></head><body>
      <script id="__NEXT_DATA__" type="application/json">
      {
        "props": {
          "pageProps": {
            "searchResults": {
              "data": [
                {
                  "title": "CORE Hook Article 2026",
                  "downloadUrl": "https://core.ac.uk/download/123.pdf",
                  "abstract": "A hook parsed abstract DOI 10.8888/core.hook",
                  "publisher": "CORE Publisher"
                }
              ]
            }
          }
        }
      }
      </script>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    items = COREConnector()._extract_from_html("machine learning", soup, limit=3)

    assert len(items) == 1
    assert items[0].doi == "10.8888/core.hook"
    assert items[0].journal == "CORE Publisher"
