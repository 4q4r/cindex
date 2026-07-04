from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

from apps.core.text import normalize_scholarly_text
from apps.ingestion.connectors import BaseConnector


class LawfulFullTextResolver:
    """Resolve full text from lawful OA endpoints only."""

    REQUEST_TIMEOUT_SECONDS = 25

    def __init__(self, connector: Any) -> None:
        """Store the source connector used for transport helpers."""
        self.connector = connector

    @staticmethod
    def _email() -> str:
        """Return the contact email required by Unpaywall."""
        configured = os.getenv("UNPAYWALL_EMAIL", "").strip()
        return configured or "cindex@app.local"

    def resolve(self, raw: Any, existing_text: str = "") -> str:
        """Resolve or augment full text for a raw article."""
        best_text = normalize_scholarly_text(existing_text)
        ocr_language = self._ocr_language(getattr(raw, "language", ""))
        for candidate_url in self._candidate_urls(raw):
            candidate_text = self._fetch_url_text(
                candidate_url, ocr_language=ocr_language,
            )
            if not candidate_text:
                continue
            merged_text = normalize_scholarly_text(
                " ".join([best_text, candidate_text]).strip(),
            )
            if len(merged_text) > len(best_text):
                best_text = merged_text
        return best_text

    def _candidate_urls(self, raw: Any) -> list[str]:
        """Build an ordered list of lawful full-text candidate URLs."""
        urls: list[str] = []
        doi = str(getattr(raw, "doi", "") or "").strip()
        if doi:
            urls.extend(self._unpaywall_candidate_urls(doi))
            urls.extend(self._europe_pmc_candidate_urls(doi))
        unique_urls: list[str] = []
        seen: set[str] = set()
        for url in urls:
            cleaned = str(url or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique_urls.append(cleaned)
        return unique_urls

    async def _unpaywall_candidate_urls_async(self, doi: str) -> list[str]:
        """Return OA URLs from Unpaywall for a DOI (async)."""
        url = (
            f"https://api.unpaywall.org/v2/{quote_plus(doi)}"
            f"?email={quote_plus(self._email())}"
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "cindex/1.0"},
                    timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_SECONDS),
                ) as response,
            ):
                response.raise_for_status()
                payload = await response.json()
        except (aiohttp.ClientError, ValueError, RuntimeError):
            return []
        if not isinstance(payload, dict):
            return []
        urls: list[str] = []
        oa_locations = payload.get("oa_locations", [])
        if not isinstance(oa_locations, list):
            oa_locations = []
        for location in [payload.get("best_oa_location"), *oa_locations]:
            if not isinstance(location, dict):
                continue
            for key in ("url_for_pdf", "url_for_landing_page", "url", "pdf_url"):
                candidate = str(location.get(key) or "").strip()
                if candidate.startswith("http"):
                    urls.append(candidate)
        return urls

    def _unpaywall_candidate_urls(self, doi: str) -> list[str]:
        """Return OA URLs from Unpaywall for a DOI (sync wrapper)."""
        return asyncio.run(self._unpaywall_candidate_urls_async(doi))

    @staticmethod
    def _extract_europe_pmc_urls(payload: dict) -> list[str]:
        """Extract full-text URLs from a Europe PMC JSON payload."""
        result_list = payload.get("resultList", {})
        if not isinstance(result_list, dict):
            return []
        results = result_list.get("result", [])
        if not isinstance(results, list) or not results:
            return []
        first = results[0]
        if not isinstance(first, dict):
            return []
        urls: list[str] = []
        url_list = first.get("fullTextUrlList", {})
        if isinstance(url_list, dict):
            full_text_urls = url_list.get("fullTextUrl", [])
            if isinstance(full_text_urls, list):
                for item in full_text_urls:
                    if not isinstance(item, dict):
                        continue
                    candidate = str(item.get("url") or "").strip()
                    if candidate.startswith("http"):
                        urls.append(candidate)
        return urls

    async def _europe_pmc_candidate_urls_async(self, doi: str) -> list[str]:
        """Return full-text URLs from Europe PMC for a DOI (async)."""
        url = (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query=DOI:%22{quote_plus(doi)}%22&format=json&pageSize=1&resultType=core"
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "cindex/1.0"},
                    timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_SECONDS),
                ) as response,
            ):
                response.raise_for_status()
                payload = await response.json()
        except (aiohttp.ClientError, ValueError, RuntimeError):
            return []
        if not isinstance(payload, dict):
            return []
        return self._extract_europe_pmc_urls(payload)

    def _europe_pmc_candidate_urls(self, doi: str) -> list[str]:
        """Return full-text URLs from Europe PMC for a DOI (sync wrapper)."""
        return asyncio.run(self._europe_pmc_candidate_urls_async(doi))

    def _ocr_language(self, language: str) -> str:
        """Map a source language to a Tesseract OCR language code."""
        normalized = normalize_scholarly_text(language).lower().strip()
        if not normalized:
            return "eng"
        mapping = {
            "ar": "ara",
            "de": "deu",
            "en": "eng",
            "eng": "eng",
            "es": "spa",
            "fr": "fra",
            "it": "ita",
            "ja": "jpn",
            "jpn": "jpn",
            "ko": "kor",
            "kor": "kor",
            "pt": "por",
            "ru": "rus",
            "rus": "rus",
            "zh": "chi_sim+chi_tra",
            "zho": "chi_sim+chi_tra",
        }
        return mapping.get(normalized, mapping.get(normalized[:2], "eng"))

    def _fetch_url_text(self, url: str, *, ocr_language: str) -> str:
        """Fetch text from a lawful OA URL using the connector transport."""
        if not url.startswith("http"):
            return ""
        try:
            _, response, body = self.connector._request_response(
                url,
                params=None,
                accept="text/html,application/xhtml+xml,application/pdf,*/*",
            )
            content_type = str(response.headers.get("Content-Type", ""))
            body_bytes = bytes(response.content or b"")
            if self.connector._is_pdf_response(url, content_type, body_bytes):
                pdf_text = self.connector._extract_pdf_text_with_language(
                    body_bytes,
                    ocr_language=ocr_language,
                )
                if pdf_text:
                    return normalize_scholarly_text(pdf_text)
            if body:
                soup = BaseConnector._sanitize_html_soup(BeautifulSoup(body, "lxml"))
                text = BaseConnector._html_text(soup)
                pdf_url = self.connector._extract_pdf_url(
                    soup,
                    url,
                    body,
                    text,
                )
                if pdf_url and pdf_url != url:
                    try:
                        pdf_text = self.connector._request_pdf_text(
                            pdf_url,
                            ocr_language=ocr_language,
                        )
                    except (ValueError, RuntimeError, ConnectionError):
                        pdf_text = ""
                    if pdf_text:
                        return normalize_scholarly_text(pdf_text)
                return text
        except (ValueError, RuntimeError, ConnectionError):
            return ""
        return ""
