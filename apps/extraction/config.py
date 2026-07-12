"""Environment-driven configuration for the PERELMAN LLM extraction pipeline.

Mirrors the established ``CINDEX_*`` ``os.getenv`` pattern (e.g.
``CINDEX_BROWSER_URL`` in ``apps/ingestion/connectors/base.py``). No default is
provided for the three required endpoint knobs (``CINDEX_LLM_BASE_URL`` /
``CINDEX_LLM_API_KEY`` / ``CINDEX_LLM_MODEL``): a missing one is a loud
configuration error (:class:`LLMNotConfiguredError`), never a silent fake
fallback. Optional tuning knobs carry safe production defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


class LLMNotConfiguredError(RuntimeError):
    """Raised when the LLM endpoint is not fully configured via env.

    A loud failure — the caller (``QuoteExtractionService``) catches it once,
    logs a single warning, and degrades to the real abstract-preview fallback
    rather than fabricating quotes.
    """


def _env_float(key: str, default: float) -> float:
    """Parse a float env var, falling back to ``default`` on any parse error."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    """Parse an int env var, falling back to ``default`` on any parse error."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_json(key: str, default: dict) -> dict:
    """Parse a JSON-object env var, falling back to ``default`` on any error."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, dict) else default


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Resolved LLM endpoint configuration (env-driven, immutable).

    Required (no defaults): ``base_url``, ``api_key``, ``model``.
    Optional: tuning + multimodal/vision knobs. The ``extra_body`` mapping is
    merged into every chat-completions request body so provider-specific
    extensions (thinking budgets, provider routing) pass through verbatim.
    """

    base_url: str
    api_key: str
    model: str
    extra_body: dict = field(default_factory=dict)
    timeout: float = 120.0
    temperature: float = 0.2
    max_quotes: int = 3
    concurrency: int = 4
    # Minimum gap between successive LLM request *starts* (seconds), enforced
    # client-side by ``OpenAICompatibleClient``. 0.0 disables the gate. Some
    # OpenAI-compatible providers (notably Z.AI's free tier) throttle by
    # request frequency (~1 QPS) on top of concurrency — set this to honour it
    # so the provider does not return 429 / drop requests.
    min_request_interval: float = 0.0
    max_input_chars: int = 12000
    articles_dir: str = "var/articles"
    # Multimodal / vision (PERELMAN receives rendered PDF pages, full-page
    # screenshots, and figure images; uses zoom/crop/rotate tools to inspect
    # small regions before transcribing formulas/graphs).
    pdf_dpi: int = 200
    max_pdf_pages: int = 8
    max_images: int = 6
    max_image_dim: int = 2000
    image_quality: int = 85
    max_tool_turns: int = 6
    image_detail: str = "high"

    def is_configured(self) -> bool:
        """Return ``True`` when all three required knobs are non-empty."""
        return bool(self.base_url and self.api_key and self.model)

    def require(self) -> LLMConfig:
        """Return ``self`` or raise :class:`LLMNotConfiguredError`."""
        missing = [
            name
            for name, value in (
                ("CINDEX_LLM_BASE_URL", self.base_url),
                ("CINDEX_LLM_API_KEY", self.api_key),
                ("CINDEX_LLM_MODEL", self.model),
            )
            if not value
        ]
        if missing:
            msg = (
                "PERELMAN quote extraction is not configured: missing "
                + ", ".join(missing)
                + ". Set them in the environment, or leave unset to degrade "
                "to the abstract preview."
            )
            raise LLMNotConfiguredError(msg)
        return self


def load_config() -> LLMConfig:
    """Build an :class:`LLMConfig` from the current environment.

    Always returns a config object (never raises) so callers can cheaply probe
    :meth:`LLMConfig.is_configured` / call :meth:`LLMConfig.require` as needed.
    """
    return LLMConfig(
        base_url=os.getenv("CINDEX_LLM_BASE_URL") or "",
        api_key=os.getenv("CINDEX_LLM_API_KEY") or "",
        model=os.getenv("CINDEX_LLM_MODEL") or "",
        extra_body=_env_json("CINDEX_LLM_EXTRA_BODY", {}),
        timeout=_env_float("CINDEX_LLM_TIMEOUT", 120.0),
        temperature=_env_float("CINDEX_LLM_TEMPERATURE", 0.2),
        max_quotes=_env_int("CINDEX_LLM_MAX_QUOTES", 3),
        concurrency=_env_int("CINDEX_LLM_CONCURRENCY", 4),
        min_request_interval=_env_float("CINDEX_LLM_MIN_REQUEST_INTERVAL", 0.0),
        max_input_chars=_env_int("CINDEX_LLM_MAX_INPUT_CHARS", 12000),
        articles_dir=os.getenv("CINDEX_ARTICLES_DIR", "var/articles"),
        pdf_dpi=_env_int("CINDEX_LLM_PDF_DPI", 200),
        max_pdf_pages=_env_int("CINDEX_LLM_MAX_PDF_PAGES", 8),
        max_images=_env_int("CINDEX_LLM_MAX_IMAGES", 6),
        max_image_dim=_env_int("CINDEX_LLM_MAX_IMAGE_DIM", 2000),
        image_quality=_env_int("CINDEX_LLM_IMAGE_QUALITY", 85),
        max_tool_turns=_env_int("CINDEX_LLM_MAX_TOOL_TURNS", 6),
        image_detail=os.getenv("CINDEX_LLM_IMAGE_DETAIL", "high"),
    )
