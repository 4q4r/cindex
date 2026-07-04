"""Live query execution and result aggregation."""

from __future__ import annotations

import os

SOURCE_QUERY_MATRIX: dict[str, list[str]] = {
    "openalex": ["machine learning", "medical imaging"],
    "crossref": ["machine learning", "public health"],
    "pubmed": ["machine learning medical diagnostics", "cancer biomarkers"],
    "arxiv": ["machine learning", "neural networks"],
    "cinii": ["machine learning", "medical imaging"],
    "cyberleninka": ["машинное обучение медицина", "machine learning diagnostics"],
    "mathnet": ["probability", "probability"],
    "europe_pmc": ["diabetes", "cancer"],
    "exa": ["machine learning", "neuroscience"],
    "doaj": ["machine learning medical diagnostics", "public health"],
    "scielo": ["machine learning", "diagnostico medico"],
    "persee": ["sociologie", "histoire sociale"],
    "ajol": ["machine learning medical diagnostics", "public health"],
}

if not os.getenv("EXA_API_KEY", "").strip():
    SOURCE_QUERY_MATRIX.pop("exa", None)

REQUIRED_SOURCES: list[str] = list(SOURCE_QUERY_MATRIX.keys())
