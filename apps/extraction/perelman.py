"""
PERELMAN-method multimodal quote extraction (query-agnostic, never raises).

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
from .llm_client import LLMRateLimitedError

if TYPE_CHECKING:
    from apps.articles.models import Article

    from .config import LLMConfig
    from .content_fetcher import ArticleContentFetcher, ContentParts
    from .llm_client import OpenAICompatibleClient

logger = structlog.get_logger(__name__)

# Final-JSON fence stripper: tolerates ```json ... ``` / ``` ... ``` / bare JSON.
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)

# Per-turn retries on a TRANSIENT upstream rate limit (Z.AI 1302 concurrency /
# 1303 frequency / 1305 server overload). The client already backs off per
# request (up to 5 attempts for 1305); if the whole route is still throttled
# after that, the request surfaces as ``LLMRateLimitedError``. Rather than
# abandon the article («Временная недоступность? Ждем, повторяем»), we retry
# the SAME turn a few more times — each retry gets a fresh per-request backoff
# budget, so a long overload spike (tens of seconds) can clear without losing
# the article. Terminal codes (1304/1308/1309/1310 — quota / window / plan) are
# NOT retried here: they propagate immediately so we don't burn the window.
_MAX_TURN_RETRIES = 3

# Raw control char -> its JSON short escape, used inside string values only.
# Other control bytes fall back to ``\uXXXX``. Structural whitespace (newlines
# / tabs *between* tokens) is left untouched by the scanner.
_CONTROL_SHORT_ESCAPES = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}

# Highest control char that must be escaped inside a JSON string value.
_CONTROL_MAX = 0x1F


def _lenient_json_loads(content: str) -> object | None:
    r"""
    Tolerant JSON loader that escapes raw control chars inside string values.

    Even with ``response_format={"type": "json_object"}`` the GLM vision model
    still emits a literal raw control character (typically 0x0A newline) inside
    a verbatim quote — ``"text": "line one<0x0A>line two"`` — which
    ``json.loads`` rejects as ``Invalid control character``. ``json_object`` does
    NOT reliably escape these, so this scanner repairs that one defect class.

    Backslashes are NOT repaired here. The finalize turn sets
    ``response_format=json_object`` so the provider doubles LaTeX backslashes
    (``\\alpha``), which is the robust class-1 fix. A lax class-1 doubling in the
    scanner was fundamentally unsafe: it could not distinguish a genuine
    ``\n`` / ``\b`` / ``\f`` / ``\r`` / ``\t`` JSON escape followed by an ASCII
    letter (a real newline + text, common in multi-line verbatim quotes after
    ``json_object``) from a LaTeX command (``\nabla`` / ``\nu``), and corrupted
    verbatim quote text — the core PERELMAN deliverable. So this scanner only
    escapes raw control chars; a bare ``\alpha`` without ``json_object`` still
    raises ``Invalid \escape`` and yields ``None`` (a safe empty result, never
    silent corruption).

    Scans char-by-char, tracking string state (toggling on an unescaped ``"``).
    Outside strings, content passes through unchanged (raw newlines are valid
    structural whitespace). Inside strings: a backslash plus the next char is
    copied verbatim — so ``\"`` does not end the string, real ``\n`` / ``\t``
    escapes survive, genuine ``\uXXXX`` is kept, and even an invalid ``\a`` is
    left for strict to reject — and a raw control char becomes its JSON escape.

    Accepted tradeoff: without ``json_object`` a bare LaTeX command whose
    escape letter is INVALID (``\alpha`` / ``\sum``) makes strict raise and
    degrade to an empty result (safe); but a bare command whose letter COLLIDES
    with a valid JSON short escape (``\beta`` / ``\nabla`` / ``\frac`` / ``\tau``
    / ``\rho``) is read by strict as a control char with NO error — a silent
    corruption of that formula. This only affects the rare no-``json_object``
    path (a provider rejects ``response_format``): production always finalizes
    with ``json_object``, which doubles the backslashes so strict parses the
    LaTeX verbatim. Re-introducing class-1 doubling in the scanner to repair
    this would re-corrupt real ``\n``+letter escapes in verbatim quotes
    (Finding 1), so the tradeoff is kept: corrupt a rare bare-collision formula
    rather than the core verbatim-quote deliverable.
    """
    out: list[str] = []
    append = out.append
    n = len(content)
    in_str = False
    i = 0
    while i < n:
        ch = content[i]
        if not in_str:
            if ch == '"':
                in_str = True
            append(ch)
            i += 1
            continue
        if ch == '"':  # unescaped quote ends the string
            in_str = False
            append(ch)
            i += 1
            continue
        if ch == "\\":
            # Copy the backslash and the following char verbatim: keeps real
            # ``\n``/``\t`` escapes, ``\\``, ``\"`` (so it does not toggle
            # in_str), genuine ``\uXXXX``, and even invalid ``\a`` (left for
            # strict to reject). Only escape a control char that follows a
            # backslash (e.g. ``\<0x0A>``), which is otherwise dropped raw.
            append("\\")
            i += 1
            if i < n:
                nxt = content[i]
                if ord(nxt) <= _CONTROL_MAX:
                    append(_CONTROL_SHORT_ESCAPES.get(nxt, f"\\u{ord(nxt):04x}"))
                else:
                    append(nxt)
                i += 1
            continue
        if ord(ch) <= _CONTROL_MAX:  # raw control char inside a string — escape it
            append(_CONTROL_SHORT_ESCAPES.get(ch, f"\\u{ord(ch):04x}"))
            i += 1
            continue
        append(ch)
        i += 1
    try:
        return json.loads("".join(out))
    except ValueError:
        return None


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

# Resume clause appended to the system prompt when the previous agent loop
# exhausted its tool turns without emitting a final JSON object. Mirrors the
# OpenAI Agents SDK «compaction + fresh turn budget» resume pattern: a fresh
# chat is started that carries a compact memory of the prior steps (how many
# inspection turns were spent, which images were inspected) instead of the
# full bloated tool-call history, so the model gets a clean turn budget and a
# small payload (the original images, not the zoomed derivatives) to produce
# the final JSON. The model is told NOT to call tools again — it already
# inspected enough; now it must transcribe and emit.
_RESUME_CLAUSE = """\

You are continuing an extraction that already used {prior_turns} inspection \
turns (zoom / crop / rotate on images: {inspected}) in a previous chat that \
ended without a final JSON object. Do NOT call any tools again — you have \
already inspected the article enough. Using what you already gathered, \
transcribe the formulas and figures and extract the verbatim quotes from the \
article text and images below, then emit the final JSON object."""

_RESUME_INSTRUCTION = (
    "Output ONLY the final JSON object (no prose, no code fences) using "
    "exactly the shape from the system prompt. Every backslash inside a "
    "string value (e.g. LaTeX) MUST be doubled (write \\\\alpha, not \\alpha) "
    "so the JSON is valid."
)


class PerelmanExtractor:
    """
    Drive the PERELMAN agent loop for one article (never raises).

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
        final_content = await self._agent_loop(messages, reg, parts, article)
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
        """
        Assemble the system + multimodal user messages (no query).

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
        parts: ContentParts,
        article: Article,
    ) -> str:
        """
        Run the bounded tool-calling loop, returning the final content string.

        ``reg`` must already contain every image referenced in the initial user
        message; tool-produced images are registered here under fresh ids. The
        loop runs at most ``cfg.max_tool_turns + 1`` chat calls: a turn that
        returns ``tool_calls`` dispatches them and continues; a turn without
        ``tool_calls`` is the final answer. If the loop exhausts turns while
        the model keeps calling tools, a FRESH chat with a compact memory of the
        prior steps (:meth:`_resume_with_memory`) is started so the model gets a
        clean turn budget and a small payload to emit the final JSON — the
        bloated tool-call history is dropped. ``_force_finalize`` on the full
        zoomed history is the last-resort fallback when the resume yields
        nothing.
        """
        cfg = self._cfg
        max_turns = cfg.max_tool_turns
        turn = 0
        turn_retries = 0
        while turn <= max_turns:
            try:
                msg = await self._client.chat(
                    messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                )
            except LLMRateLimitedError as exc:
                # Terminal quota / window / plan exhaustion (1304/1308/1309/
                # 1310) — retrying only burns the window. Abort the article.
                if exc.terminal:
                    logger.warning(
                        "perelman: terminal rate limit, aborting extraction",
                        turn=turn,
                        code=exc.code,
                        error=str(exc)[:150],
                    )
                    return ""
                # Transient (1302/1303/1305): the client already exhausted its
                # per-request backoff budget. Retry the SAME turn a bounded
                # number of times so a long overload spike can clear instead
                # of zeroing the article («Временная недоступность? Ждем,
                # повторяем»). Each retry gets a fresh per-request budget.
                if turn_retries >= _MAX_TURN_RETRIES:
                    # Per the user directive ("at the limit, start a new chat
                    # with a memory of the prior steps"): when the turn retry
                    # budget is also exhausted, do NOT abandon the article —
                    # start a FRESH tool-free chat with a compact memory of the
                    # prior inspection turns. The resume payload is tiny
                    # (original images, no zoomed derivatives, no tool history)
                    # and is a single request, so it is far more likely to
                    # clear a lingering 1302/1305 than another retry of the
                    # bloated tool-call history. ``prior_turns=turn`` because
                    # this turn never produced an assistant message (chat
                    # raised first).
                    logger.warning(
                        "perelman: transient overload exhausted turn retries, "
                        "falling back to fresh-chat resume",
                        turn=turn,
                        retries=turn_retries,
                        code=exc.code,
                    )
                    return await self._finalize_after_exhaustion(
                        messages,
                        parts,
                        article,
                        reg,
                        turn,
                    )
                turn_retries += 1
                logger.info(
                    "perelman: transient overload, retrying turn",
                    turn=turn,
                    retry=turn_retries,
                    code=exc.code,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - per-result isolation
                logger.warning("perelman: llm call failed", turn=turn, error=str(exc))
                return ""
            # Successful chat call — reset the transient-retry budget for the
            # next turn.
            turn_retries = 0
            messages.append(msg)
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                content = msg.get("content")
                return content if isinstance(content, str) else ""
            # Dispatch tool calls on every turn (including the last) so the
            # conversation stays well-formed: an assistant message carrying
            # tool_calls must be followed by tool-result messages, or the
            # next request is rejected by strict OpenAI-compatible providers.
            await self._handle_tool_calls(tool_calls, reg, messages)
            if turn == max_turns:
                logger.warning(
                    "perelman: max_tool_turns reached with pending tool_calls",
                    max_turns=max_turns,
                )
                return await self._finalize_after_exhaustion(
                    messages,
                    parts,
                    article,
                    reg,
                    max_turns,
                )
            turn += 1
        return ""  # pragma: no cover - loop always returns or raises above

    async def _finalize_after_exhaustion(
        self,
        messages: list[dict],
        parts: ContentParts,
        article: Article,
        reg: ImageRegistry,
        prior_turns: int,
    ) -> str:
        """
        Resume in a fresh chat with memory; fall back to zoomed-history finalize.

        The fresh-chat resume is primary (matches the OpenAI Agents SDK
        «compaction + fresh turn budget» pattern: a small, clean payload — the
        original images, not the zoomed derivatives — with a compact memory of
        the prior inspection turns, no tools, and ``json_object``). If the
        resume yields nothing, :meth:`_force_finalize` on the full tool-call
        history is the last resort: it carries the zoomed detail the resume
        dropped, at the cost of a larger payload more likely to hit 1305.
        """
        content = await self._resume_with_memory(parts, article, reg, prior_turns)
        if content and content.strip():
            return content
        logger.info(
            "perelman: resume empty, falling back to zoomed-history finalize",
        )
        return await self._force_finalize(messages)

    async def _resume_with_memory(
        self,
        parts: ContentParts,
        article: Article,
        reg: ImageRegistry,
        prior_turns: int,
    ) -> str:
        r"""
        Start a FRESH tool-free chat that resumes the exhausted extraction.

        Builds a brand-new 2-message list (system + user) — NOT appended to the
        bloated tool-call history — so the model gets a clean turn budget and a
        small payload. The system prompt carries a resume clause summarizing
        the prior steps (turns spent + which images were inspected); the user
        message re-sends the original images and the article text plus an
        explicit «output only JSON, double backslashes» instruction. Tools are
        stripped (``tools=None``), so the model physically cannot call them.

        ``response_format={"type": "json_object"}`` makes the provider validate
        the output and double LaTeX backslashes (``\\alpha``), the robust fix
        for vision models that otherwise emit single-backslash LaTeX and break
        ``json.loads``. The lax scanner in :meth:`_parse` is the fallback.

        Returns the final content string (possibly empty). Never raises.
        """
        system = (
            _SYSTEM_PROMPT.format(max_quotes=self._cfg.max_quotes)
            + "\n\n"
            + _RESUME_CLAUSE.format(
                prior_turns=prior_turns,
                inspected=", ".join(img.id for img in parts.images) or "(none)",
            )
        )
        image_index = self._image_index(parts)
        metadata = self._metadata(article)
        user_text = (
            f"{metadata}\n\n{image_index}\n\nARTICLE TEXT:\n{parts.text}\n\n"
            f"{_RESUME_INSTRUCTION}"
        )
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
        resume_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        try:
            msg = await self._client.chat(
                resume_messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - per-result isolation
            # Some providers reject ``response_format`` with a 400. Retry once
            # without it; the lenient scanner in :meth:`_parse` still copes.
            logger.warning(
                "perelman: json_object resume rejected, retrying plain",
                error=str(exc),
            )
            try:
                msg = await self._client.chat(resume_messages)
            except Exception as exc2:  # noqa: BLE001 - per-result isolation
                logger.warning("perelman: resume chat failed", error=str(exc2))
                return ""
        if msg.get("tool_calls"):
            logger.warning(
                "perelman: resume call returned tool_calls despite stripped tools",
            )
        content = msg.get("content")
        return content if isinstance(content, str) else ""

    async def _force_finalize(self, messages: list[dict]) -> str:
        r"""
        Force a tool-free final answer when the tool loop is exhausted.

        Over-eager vision models (e.g. Z.AI's ``glm-4.6v-flash``) keep calling
        ``zoom`` / ``crop`` / ``rotate`` on every turn and never emit a final
        JSON object, so ``_agent_loop`` would return empty content and the
        article would get zero quotes. This strips the tools entirely (no
        ``tools`` / ``tool_choice`` — the model physically cannot call tools)
        and sends an explicit «finalize now» instruction, letting the model
        transcribe what it has already inspected into the required JSON.

        ``response_format={"type": "json_object"}`` is set so the provider
        validates the output as JSON and emits LaTeX backslashes already doubled
        (``\\alpha``), which is the robust fix for vision models that otherwise
        write single-backslash LaTeX and break ``json.loads`` with
        ``Invalid \\escape``. The lax-backslash repair in :meth:`_parse` is kept
        as a fallback for providers that ignore or reject ``json_object``.

        Returns the final content string (possibly empty on failure). Never
        raises: a failure here yields ``""`` which parses to an empty result.
        """
        messages.append(
            {
                "role": "user",
                "content": (
                    "You have used all available inspection turns. Stop calling "
                    "tools and return the final JSON object NOW with the quotes, "
                    "formulas, and figures you have gathered so far. Use exactly "
                    "the shape from the system prompt and output ONLY the JSON "
                    "object (no prose, no code fences). Every backslash inside a "
                    "string value (e.g. LaTeX) MUST be doubled (write \\\\alpha, "
                    "not \\alpha) so the JSON is valid."
                ),
            },
        )
        try:
            msg = await self._client.chat(
                messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - per-result isolation
            # Some OpenAI-compatible providers reject ``response_format`` with a
            # 400. Retry once without it so the model can still finalize: the
            # lenient scanner in :meth:`_parse` escapes any raw control chars,
            # and bare LaTeX (without json_object doubling) degrades to an empty
            # result rather than corrupting the quotes.
            logger.warning(
                "perelman: json_object finalize rejected, retrying plain",
                error=str(exc),
            )
            try:
                msg = await self._client.chat(messages)
            except Exception as exc2:  # noqa: BLE001 - per-result isolation
                logger.warning("perelman: finalize call failed", error=str(exc2))
                return ""
        # A well-behaved provider returns plain content with tools stripped. If
        # it still returns tool_calls (ignoring the omitted tools), log it so
        # operators can distinguish a non-converging model from provider
        # misbehavior; the tool_calls are dropped and the result is empty.
        if msg.get("tool_calls"):
            logger.warning(
                "perelman: finalize call returned tool_calls despite stripped tools",
            )
        content = msg.get("content")
        return content if isinstance(content, str) else ""

    async def _handle_tool_calls(
        self,
        tool_calls: list[dict],
        reg: ImageRegistry,
        messages: list[dict],
    ) -> None:
        """
        Dispatch each tool call and append its result message.

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
        r"""
        Parse the LLM's final JSON content into an :class:`ExtractionResult`.

        Tolerates ```json fences and bare JSON. Two malformations reach here:
        (1) unescaped LaTeX backslashes (``\alpha``) — handled on the finalize
        turn by ``response_format=json_object`` (the provider doubles them).
        Without ``json_object``, a bare command with an INVALID escape letter
        (``\alpha`` / ``\sum``) raises ``Invalid \escape`` and degrades to an
        empty result; a bare COLLISION command (``\beta`` / ``\nabla`` / ``\frac``
        / ``\tau`` / ``\rho``) is silently misread by strict as a control char
        — an accepted tradeoff of the rare no-``json_object`` retry path
        (production always finalizes with ``json_object``, see
        :func:`_lenient_json_loads`). (2) raw control chars (literal 0x0A)
        inside string values, raising ``Invalid control character`` —
        ``json_object`` does NOT fix these, so the lenient scanner escapes them.

        Strict ``json.loads`` is the fast path (covers well-formed json_object
        output). On ``ValueError`` the lenient scanner repairs class 2. The
        scanner NEVER overrides a successful strict parse: it cannot distinguish
        a real ``\n``+letter escape (newline + text in a verbatim quote) from a
        LaTeX ``\nabla`` / ``\nu``, so any class-1 doubling on the strict path
        would corrupt verbatim quote text. The scanner runs ONLY when strict
        raises. Structural / unparseable payloads yield an empty result
        (logged), never raise.
        """
        if not content or not content.strip():
            return ExtractionResult()
        stripped = _JSON_FENCE_RE.sub("", content.strip()).strip()
        # If the model wrapped the JSON in prose, grab the outermost {...} block.
        if not stripped.startswith("{"):
            start, end = stripped.find("{"), stripped.rfind("}")
            if start != -1 and end != -1 and end > start:
                stripped = stripped[start : end + 1]
        # Fast path: well-formed JSON (incl. json_object-doubled backslashes).
        # A successful strict parse is returned AS-IS — the lenient scanner
        # cannot safely repair bare-backslash LaTeX (it would corrupt real
        # ``\n``+letter escapes in verbatim quotes), so it runs ONLY when strict
        # raises, escaping any raw control chars.
        try:
            data = json.loads(stripped)
        except ValueError:
            data = _lenient_json_loads(stripped)
            if data is None:
                logger.warning(
                    "perelman: final JSON parse failed",
                    content_len=len(content),
                    content_preview=content[:600],
                )
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
        """
        Extract for many articles concurrently with per-result isolation.

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
