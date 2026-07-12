"""PERELMAN-method multimodal quote extraction (query-agnostic, never raises).

Implements the agentic extraction at the core of the «система Перелмана»: a
vision-capable LLM receives an article as **text plus images** (rendered PDF
pages, a full-page screenshot, and/or figure images) and, following the
PERELMAN framework (arXiv 2512.21727 — agentic meta-analysis of scientific
literature), elicits the article's core contribution and returns:

* up to ``cfg.max_quotes`` **verbatim** passages (the article's salient
  claims / findings / definitions), each with a location and relevance;
* **all** mathematical formulas transcribed as LaTeX (``$...$`` / ``$$...$$``);
* **all** graphs / plots / tables / figures converted to markdown
  (description + readable axis labels / legend / data as a markdown table +
  caption) — «конвертировал в md от и до».

The agent may call ``zoom`` / ``crop`` / ``rotate`` (executed host-side by
:mod:`apps.extraction.image_ops` on pymupdf) to inspect a small region before
transcribing it. The loop is bounded by ``cfg.max_tool_turns`` and degrades to
a single-shot extraction when the provider does not support tool-calling.

**Query-agnostic:** the search query is deliberately NOT passed to the LLM.
Quotes are cached per-article (``ArticleQuotes``) and re-used across every
search, so they must capture the article's own salient passages — not be
tuned to one query. The query is used only for frontend highlighting.

**Never raises:** a single article's HTTP / JSON / tool / structure failure
yields an empty :class:`ExtractionResult` (logged), so one bad article never
aborts the batch.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pymupdf
import structlog

from .image_ops import TOOL_SCHEMAS, ImageRegistry, ToolError, dispatch

if TYPE_CHECKING:
    from apps.articles.models import Article

    from .config import LLMConfig
    from .content_fetcher import ArticleContentFetcher, ContentParts
    from .llm_client import OpenAICompatibleClient

logger = structlog.get_logger(__name__)

# Final-JSON fence stripper: tolerates ```json ... ``` / ``` ... ``` / bare JSON.
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Quote:
    """A verbatim passage extracted from the article."""

    text: str
    location: str = ""
    relevance: float = 0.0
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class Formula:
    """A formula transcribed as LaTeX with its location in the article."""

    latex: str
    location: str = ""
    caption: str = ""


@dataclass(frozen=True, slots=True)
class Figure:
    """A graph / plot / table / figure converted to markdown."""

    markdown: str
    location: str = ""
    caption: str = ""
    kind: str = "figure"


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The PERELMAN agent's parsed output for one article."""

    quotes: list[Quote] = field(default_factory=list)
    formulas: list[Formula] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Return ``True`` when nothing was extracted (no_text / failure)."""
        return not (self.quotes or self.formulas or self.figures)


_SYSTEM_PROMPT = """\
You are a scientific literature analysis agent using the PERELMAN method with \
vision. You receive an article as TEXT plus IMAGES (rendered PDF pages, a \
full-page screenshot, and/or figure images). Your tasks:

1. Elicit the article's core domain contribution — its claims, findings, key \
definitions, and methodology.
2. Extract up to {max_quotes} VERBATIM passages that best capture that \
contribution. Prioritize results, conclusions, and definitions. Quote text \
MUST be copied word-for-word from the article (you may include transcribed \
formulas inline). For each quote give its location (e.g. "abstract", \
"section 3", "page 2", "figure 1 caption") and a short rationale.
3. Transcribe ALL mathematical formulas visible in the text or images as \
LaTeX — use $...$ for inline and $$...$$ for display formulas — with their \
location and an optional caption.
4. Convert ALL graphs, plots, tables, and figures to markdown: describe the \
figure, transcribe readable axis labels / legend / data points as a markdown \
table where feasible, and include the caption. Mark the figure kind \
(figure | graph | table).
5. Use the zoom / crop / rotate tools to inspect any formula, graph region, \
or table that is too small to read at the current resolution BEFORE \
transcribing it. Each image is identified by its image_id and has known \
pixel dimensions (given below); express crop regions and zoom factors in \
those source pixels.

Return a single JSON object with exactly this shape:
{{
  "quotes": [{{"text": "...", "location": "...", "relevance": 0.0-1.0, \
"rationale": "..."}}],
  "formulas": [{{"latex": "...", "location": "...", "caption": "..."}}],
  "figures": [{{"markdown": "...", "location": "...", "caption": "...", \
"kind": "figure"}}]
}}

If a region is unreadable even after zooming, transcribe what you can and \
note the uncertainty in the caption. If the article has no extractable \
content, return all three lists empty."""


class PerelmanExtractor:
    """Drive the PERELMAN agent loop for one article (never raises).

    Constructed with a resolved LLM client, config, and a content fetcher.
    ``extract`` gathers multimodal content, runs the bounded tool-calling
    loop, and parses the final JSON into an :class:`ExtractionResult`.
    """

    def __init__(
        self,
        client: OpenAICompatibleClient,
        cfg: LLMConfig,
        fetcher: ArticleContentFetcher,
    ) -> None:
        """Bind the LLM client, config, and content fetcher."""
        self._client = client
        self._cfg = cfg
        self._fetcher = fetcher

    async def extract(self, article: Article) -> ExtractionResult:
        """Run the PERELMAN agent loop for ``article`` (never raises)."""
        try:
            parts = await asyncio.to_thread(self._fetcher.fetch, article)
        except Exception as exc:  # noqa: BLE001 - fetcher is expected not to raise
            logger.warning("perelman: content fetch failed", error=str(exc))
            return ExtractionResult()
        if parts.is_empty:
            logger.info("perelman: no extractable content", url=article.url)
            return ExtractionResult()
        reg = self._register_images(parts)
        messages = self._build_messages(parts, article, reg)
        final_content = await self._agent_loop(messages, reg)
        return self._parse(final_content)

    def _register_images(self, parts: ContentParts) -> ImageRegistry:
        """Register every gathered image in a host-side registry for tool dispatch."""
        reg = ImageRegistry()
        for img in parts.images:
            reg.register(img.id, img.data, img.mime, img.kind, img.width, img.height)
        return reg

    def _build_messages(
        self,
        parts: ContentParts,
        article: Article,
        reg: ImageRegistry,
    ) -> list[dict]:
        """Assemble the system + multimodal user messages (no query).

        Each gathered image is sent as an ``image_url`` part whose data URI is
        pulled from ``reg`` (the same registry the tool loop dispatches
        against), so image ids referenced in the text index and tool calls
        resolve to the same bytes.
        """
        system = _SYSTEM_PROMPT.format(max_quotes=self._cfg.max_quotes)
        image_index = self._image_index(parts)
        metadata = self._metadata(article)
        user_text = f"{metadata}\n\n{image_index}\n\nARTICLE TEXT:\n{parts.text}"
        user_content: list[dict] = [{"type": "text", "text": user_text}]
        user_content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": reg.data_uri(img.id),
                    "detail": self._cfg.image_detail,
                },
            }
            for img in parts.images
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

    def _image_index(self, parts: ContentParts) -> str:
        """Build the human-readable image list the LLM uses for tool calls."""
        if not parts.images:
            return "IMAGES: (none)"
        lines = ["IMAGES (image_id, source pixels, kind):"]
        lines.extend(
            f"- {img.id}: {img.width}x{img.height} ({img.kind})" for img in parts.images
        )
        return "\n".join(lines)

    def _metadata(self, article: Article) -> str:
        """Best-effort article metadata header (defensive against missing attrs)."""
        title = _safe_str(getattr(article, "title", ""))
        doi = _safe_str(getattr(article, "doi", ""))
        year = _safe_str(getattr(article, "publication_year", ""))
        bits = [f"Title: {title}"] if title else []
        if year:
            bits.append(f"Year: {year}")
        if doi:
            bits.append(f"DOI: {doi}")
        return "METADATA: " + " | ".join(bits) if bits else "METADATA: (none)"

    async def _agent_loop(
        self,
        messages: list[dict],
        reg: ImageRegistry,
    ) -> str:
        """Run the bounded tool-calling loop, returning the final content string.

        ``reg`` must already contain every image referenced in the initial user
        message; tool-produced images are registered here under fresh ids. The
        loop runs at most ``cfg.max_tool_turns + 1`` chat calls: a turn that
        returns ``tool_calls`` dispatches them and continues; a turn without
        ``tool_calls`` is the final answer. If the loop exhausts turns while
        the model keeps calling tools, the last content (possibly empty) is
        returned and parsed to an empty result.
        """
        cfg = self._cfg
        max_turns = cfg.max_tool_turns
        final_content = ""
        for turn in range(max_turns + 1):
            try:
                msg = await self._client.chat(
                    messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                )
            except Exception as exc:  # noqa: BLE001 - per-result isolation
                logger.warning("perelman: llm call failed", turn=turn, error=str(exc))
                return final_content
            messages.append(msg)
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                content = msg.get("content")
                return content if isinstance(content, str) else ""
            final_content = msg.get("content") or final_content
            if turn == max_turns:
                logger.warning(
                    "perelman: max_tool_turns reached with pending tool_calls",
                    max_turns=max_turns,
                )
                break
            await self._handle_tool_calls(tool_calls, reg, messages)
        return final_content

    async def _handle_tool_calls(
        self,
        tool_calls: list[dict],
        reg: ImageRegistry,
        messages: list[dict],
    ) -> None:
        """Dispatch each tool call and append its result message.

        A successful image-producing tool (zoom/crop/rotate) registers the
        result under ``f"{image_id}-{tool}{n}"`` and returns it as an
        ``image_url`` tool message. A :class:`ToolError` is returned to the
        model as a textual tool message so the loop can continue (retry or
        give up on that region).
        """
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments", "{}")
            messages.append(
                await self._dispatch_tool(tc.get("id", ""), name, raw_args, reg),
            )

    async def _dispatch_tool(
        self,
        tool_call_id: str,
        name: str,
        raw_args: str,
        reg: ImageRegistry,
    ) -> dict:
        """Execute one tool call, returning the tool-role message for the LLM."""
        try:
            args = json.loads(raw_args) if raw_args else {}
        except ValueError:
            args = {}
        try:
            data, mime = dispatch(reg, self._cfg, name, args)
        except ToolError as exc:
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": f"error: {exc}",
            }
        except Exception as exc:  # noqa: BLE001 - never let a tool crash the loop
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": f"error: {exc}",
            }
        new_id = self._fresh_tool_id(args.get("image_id", "tool"), name, reg)
        dims = _decode_dims_safe(data)
        reg.register(new_id, data, mime, "tool-result", dims[0], dims[1])
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Tool {name} produced image {new_id} "
                        f"({dims[0]}x{dims[1]}). Use its image_id for further "
                        "zoom/crop/rotate calls."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": reg.data_uri(new_id),
                        "detail": self._cfg.image_detail,
                    },
                },
            ],
        }

    def _fresh_tool_id(
        self,
        base_id: str,
        tool: str,
        reg: ImageRegistry,
    ) -> str:
        """Generate a unique image_id for a tool result (``{base}-{tool}{n}``)."""
        base = base_id or "img"
        n = 1
        while reg.get(f"{base}-{tool}{n}") is not None:
            n += 1
        return f"{base}-{tool}{n}"

    def _parse(self, content: str) -> ExtractionResult:
        """Parse the LLM's final JSON content into an :class:`ExtractionResult`.

        Tolerates ```json fences and bare JSON. Malformed / mistyped payloads
        yield an empty result (logged) rather than raising.
        """
        if not content or not content.strip():
            return ExtractionResult()
        stripped = _JSON_FENCE_RE.sub("", content.strip()).strip()
        # If the model wrapped the JSON in prose, grab the outermost {...} block.
        if not stripped.startswith("{"):
            start, end = stripped.find("{"), stripped.rfind("}")
            if start != -1 and end != -1 and end > start:
                stripped = stripped[start : end + 1]
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            logger.warning("perelman: final JSON parse failed", error=str(exc))
            return ExtractionResult()
        if not isinstance(data, dict):
            return ExtractionResult()
        return ExtractionResult(
            quotes=_parse_quotes(data.get("quotes")),
            formulas=_parse_formulas(data.get("formulas")),
            figures=_parse_figures(data.get("figures")),
        )

    async def extract_batch(
        self,
        articles: list[Article],
    ) -> list[ExtractionResult]:
        """Extract for many articles concurrently with per-result isolation.

        Bounded by ``cfg.concurrency`` via a semaphore; one article's failure
        yields an empty :class:`ExtractionResult` for that slot (never raises).
        """
        sem = asyncio.Semaphore(self._cfg.concurrency)

        async def _one(article: Article) -> ExtractionResult:
            async with sem:
                return await self.extract(article)

        results = await asyncio.gather(
            *[_one(a) for a in articles],
            return_exceptions=True,
        )
        out: list[ExtractionResult] = []
        for res in results:
            if isinstance(res, ExtractionResult):
                out.append(res)
            else:
                logger.warning("perelman: batch item raised", error=str(res))
                out.append(ExtractionResult())
        return out


def _safe_str(value: object) -> str:
    """Return ``str(value).strip()`` or ``""`` for ``None``."""
    if value is None:
        return ""
    return str(value).strip()


def _decode_dims_safe(data: bytes) -> tuple[int, int]:
    """Return best-effort source-pixel dims for a tool result (1x1 if undecodable)."""
    try:
        pix = pymupdf.Pixmap(data)
    except (ValueError, RuntimeError, OSError):
        return 1, 1
    return pix.width, pix.height


def _parse_quotes(raw: object) -> list[Quote]:
    """Coerce a raw ``quotes`` value into a list of :class:`Quote`."""
    if not isinstance(raw, list):
        return []
    out: list[Quote] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = _safe_str(item.get("text"))
        if not text:
            continue
        out.append(
            Quote(
                text=text,
                location=_safe_str(item.get("location")),
                relevance=_clamp01(item.get("relevance")),
                rationale=_safe_str(item.get("rationale")),
            ),
        )
    return out


def _parse_formulas(raw: object) -> list[Formula]:
    """Coerce a raw ``formulas`` value into a list of :class:`Formula`."""
    if not isinstance(raw, list):
        return []
    out: list[Formula] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        latex = _safe_str(item.get("latex"))
        if not latex:
            continue
        out.append(
            Formula(
                latex=latex,
                location=_safe_str(item.get("location")),
                caption=_safe_str(item.get("caption")),
            ),
        )
    return out


def _parse_figures(raw: object) -> list[Figure]:
    """Coerce a raw ``figures`` value into a list of :class:`Figure`."""
    if not isinstance(raw, list):
        return []
    out: list[Figure] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        markdown = _safe_str(item.get("markdown"))
        if not markdown:
            continue
        out.append(
            Figure(
                markdown=markdown,
                location=_safe_str(item.get("location")),
                caption=_safe_str(item.get("caption")),
                kind=_safe_str(item.get("kind")) or "figure",
            ),
        )
    return out


def _clamp01(value: object) -> float:
    """Coerce ``value`` to a float in ``[0, 1]`` (0.0 on any failure)."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))
