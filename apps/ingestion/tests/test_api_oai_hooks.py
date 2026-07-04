from apps.ingestion.connectors import COREConnector, DergiParkConnector


def test_core_v3_payload_parser() -> None:
    """Test core v 3 payload parser helper."""
    payload = {
        "results": [
            {
                "id": 123,
                "title": "CORE API Hook 2025",
                "abstract": "Study on machine learning DOI 10.4444/core.api.1",
                "downloadUrl": "https://core.ac.uk/download/123.pdf",
                "publisher": "CORE Journal",
                "yearPublished": 2025,
                "doi": "10.4444/core.api.1",
            }
        ]
    }
    items = COREConnector()._extract_from_payload("machine learning", payload, limit=2)
    assert len(items) == 1
    assert items[0].doi == "10.4444/core.api.1"
    assert items[0].year == 2025


def test_dergipark_oai_record_parser() -> None:
    """Test dergipark oai record parser helper."""
    xml = """<?xml version='1.0' encoding='UTF-8'?>
<OAI-PMH xmlns='http://www.openarchives.org/OAI/2.0/'>
  <ListRecords>
    <record>
      <metadata>
        <dc:dc xmlns:dc='http://purl.org/dc/elements/1.1/'>
          <dc:title>DergiPark OAI Hook 2024</dc:title>
          <dc:description>
            Machine learning peer reviewed article DOI 10.5555/dergi.1
          </dc:description>
          <dc:date>2024-07-01</dc:date>
          <dc:identifier>https://dergipark.org.tr/en/pub/demo/issue/1/1</dc:identifier>
          <dc:identifier>10.5555/dergi.1</dc:identifier>
        </dc:dc>
      </metadata>
    </record>
  </ListRecords>
</OAI-PMH>
"""
    items = DergiParkConnector()._parse_oai_records(
        xml, "machine learning", "Demo Journal", 3
    )
    assert len(items) == 1
    assert items[0].journal == "Demo Journal"
    assert items[0].doi == "10.5555/dergi.1"
