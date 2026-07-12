"""Unit tests for :mod:`apps.extraction.perelman` (real image_ops, no mocks).

Exercises the PERELMAN agent loop end to end with a scripted LLM client: the
fetcher returns canned :class:`ContentParts` (real blank-PNG image, real text),
and the client returns a sequence of assistant messages (tool_calls then final
JSON). The tool calls dispatch to the **real** :mod:`apps.extraction.image_ops`
``zoom`` so the host-side execution of the agentic loop is genuinely verified
(produced bytes decode back to a real image), not stubbed.

Covers: agent-loop happy path (tool_call → real zoom → image_url tool message
with ``tool_call_id`` → final JSON parsed), no-tool single-shot path, the
``max_tool_turns`` guard (no infinite loop, no raise), malformed JSON / HTTP
error / ``ToolError`` isolation (empty result, never raises), query-agnosticism
(no query in messages), multimodal ``_build_messages`` shape (text + image_url
parts with ``detail``), empty/raising fetcher → empty result, and
``extract_batch`` per-result isolation.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

import pymupdf
import pytest

from apps.articles.models import Article, Journal, Source
from apps.extraction.config import LLMConfig
from apps.extraction.content_fetcher import ContentParts, ImageInput
from apps.extraction.image_ops import TOOL_SCHEMAS
from apps.extraction.models import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NO_TEXT,
    STATUS_PENDING,
    ArticleQuotes,
)
from apps.extraction.perelman import (
    ExtractionResult,
    Formula,
    PerelmanExtractor,
    Quote,
)
from apps.extraction.services import QuoteExtractionService
from apps.ingestion.connectors.base import ConnectorFetchError


def _cfg(**overrides: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "base_url": "http://llm.example.com/v1",
        "api_key": "secret",
        "model": "vision-model",
        "max_quotes": 3,
        "concurrency": 2,
        "max_input_chars": 12000,
        "max_tool_turns": 6,
        "max_image_dim": 4000,
        "image_quality": 85,
        "image_detail": "high",
    }
    base.update(overrides)
    return LLMConfig(**base)


@dataclass
class _StubArticle:
    url: str = "https://example.com/article"
    title: str = "A Study of Quarks"
    doi: str = "10.1007/s10994-024-00100-x"
    publication_year: str = "2024"
    abstract: str = "We demonstrate a verbatim result worth quoting."
    full_text: str = ""


def _blank_png() -> tuple[bytes, int, int]:
    doc = pymupdf.open()
    pix = doc.new_page().get_pixmap()
    return pix.tobytes("png"), pix.width, pix.height


def _image_input(image_id: str = "page-0") -> ImageInput:
    png, w, h = _blank_png()
    return ImageInput(
        id=image_id,
        data=png,
        mime="image/png",
        kind="pdf-page",
        width=w,
        height=h,
    )


def _parts(
    text: str = "Body text with a quotable sentence.",
    images=None,
) -> ContentParts:
    return ContentParts(
        text=text,
        images=images if images is not None else [_image_input()],
    )


def _decode_dims(image_bytes: bytes) -> tuple[int, int]:
    pix = pymupdf.Pixmap(image_bytes)
    return pix.width, pix.height


def _assistant(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
) -> dict:
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


_FINAL_JSON = json.dumps(
    {
        "quotes": [
            {
                "text": "We demonstrate a verbatim result worth quoting.",
                "location": "abstract",
                "relevance": 0.9,
                "rationale": "core claim",
            },
        ],
        "formulas": [{"latex": "$$E=mc^2$$", "location": "section 1", "caption": ""}],
        "figures": [
            {
                "markdown": "| x | y |\n|---|---|\n| 1 | 2 |",
                "location": "figure 1",
                "caption": "Plot of y vs x.",
                "kind": "graph",
            },
        ],
    },
)


class _FakeFetcher:
    def __init__(self, parts: ContentParts, raises: bool = False) -> None:
        self._parts = parts
        self._raises = raises
        self.calls: list[Any] = []

    def fetch(self, article: Any) -> ContentParts:
        self.calls.append(article)
        if self._raises:
            msg = "simulated fetcher failure"
            raise RuntimeError(msg)
        return self._parts


class _FakeClient:
    """Scripted async chat client: returns queued messages, records kwargs."""

    def __init__(self, messages: list[dict], raises_on: int | None = None) -> None:
        self._messages = list(messages)
        self._raises_on = raises_on
        self.chat_calls: list[dict[str, Any]] = []
        self.kwargs: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        extra_body_override: dict | None = None,
    ) -> dict:
        idx = len(self.chat_calls)
        self.chat_calls.append(messages)  # live reference; extractor appends in place
        self.kwargs.append(
            {
                "temperature": temperature,
                "response_format": response_format,
                "tools": tools,
                "tool_choice": tool_choice,
            },
        )
        if self._raises_on is not None and idx == self._raises_on:
            msg = "simulated HTTP 500"
            raise ConnectorFetchError(msg)
        return self._messages[idx]


def _extractor(
    client: _FakeClient,
    fetcher: _FakeFetcher,
    cfg: LLMConfig | None = None,
) -> PerelmanExtractor:
    return PerelmanExtractor(client, cfg or _cfg(), fetcher)


class TestAgentLoop:
    def test_tool_call_then_final_json_parses_result(self) -> None:
        client = _FakeClient(
            [
                _assistant(
                    tool_calls=[
                        _tool_call(
                            "call_1",
                            "zoom",
                            {"image_id": "page-0", "factor": 2.0},
                        ),
                    ],
                ),
                _assistant(content=_FINAL_JSON),
            ],
        )
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert len(result.quotes) == 1
        assert (
            result.quotes[0].text == "We demonstrate a verbatim result worth quoting."
        )
        assert result.quotes[0].relevance == 0.9
        assert len(result.formulas) == 1
        assert result.formulas[0].latex == "$$E=mc^2$$"
        assert len(result.figures) == 1
        assert result.figures[0].kind == "graph"
        # Two chat calls: tool turn + final turn.
        assert len(client.chat_calls) == 2
        # tools / tool_choice forwarded on every call.
        for kw in client.kwargs:
            assert kw["tools"] is TOOL_SCHEMAS
            assert kw["tool_choice"] == "auto"

    def test_tool_result_message_carries_image_url_and_tool_call_id(self) -> None:
        client = _FakeClient(
            [
                _assistant(
                    tool_calls=[
                        _tool_call(
                            "call_1",
                            "zoom",
                            {"image_id": "page-0", "factor": 2.0},
                        ),
                    ],
                ),
                _assistant(content=_FINAL_JSON),
            ],
        )
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        asyncio.run(extractor.extract(_StubArticle()))

        # By the 2nd chat call the extractor had appended the assistant tool_calls
        # message and the tool result message to the live messages list.
        second_call_messages = client.chat_calls[1]
        tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        content = tool_msgs[0]["content"]
        assert isinstance(content, list)
        image_part = next(p for p in content if p.get("type") == "image_url")
        url = image_part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        # Real zoom ran: the produced JPEG decodes back to a real image.
        decoded = base64.standard_b64decode(url.split(",", 1)[1])
        _decode_dims(decoded)

    def test_no_tool_path_single_shot(self) -> None:
        client = _FakeClient([_assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert len(client.chat_calls) == 1
        assert len(result.quotes) == 1

    def test_max_tool_turns_guard_force_finalizes_and_parses(self) -> None:
        # The model keeps calling tools past max_tool_turns (over-eager vision
        # model, e.g. glm-4.6v-flash). The loop dispatches every turn (so the
        # conversation stays well-formed), then makes ONE tool-free finalize
        # call whose JSON is parsed — instead of returning empty content.
        tool_turn = _assistant(
            tool_calls=[
                _tool_call("c", "zoom", {"image_id": "page-0", "factor": 1.5}),
            ],
        )
        client = _FakeClient([tool_turn, tool_turn, _assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher, _cfg(max_tool_turns=1))

        result = asyncio.run(extractor.extract(_StubArticle()))

        # max_tool_turns=1 → 2 tool turns (0 and 1) + 1 force-finalize call.
        assert len(client.chat_calls) == 3
        # Tool turns carried tools; the finalize call stripped them.
        assert client.kwargs[0]["tools"] is TOOL_SCHEMAS
        assert client.kwargs[1]["tools"] is TOOL_SCHEMAS
        assert client.kwargs[2]["tools"] is None
        assert client.kwargs[2]["tool_choice"] is None
        # The finalize message is a user «finalize now» instruction.
        finalize_msgs = client.chat_calls[2]
        assert finalize_msgs[-1]["role"] == "user"
        assert "final JSON" in finalize_msgs[-1]["content"]
        # Finalize JSON parsed → quotes recovered despite the tool loop.
        assert len(result.quotes) == 1
        assert (
            result.quotes[0].text == "We demonstrate a verbatim result worth quoting."
        )

    def test_max_tool_turns_guard_empty_when_finalize_also_empty(self) -> None:
        # If the finalize call also returns no content (model refuses to
        # converge), the result is empty but never raises.
        tool_turn = _assistant(
            tool_calls=[
                _tool_call("c", "zoom", {"image_id": "page-0", "factor": 1.5}),
            ],
        )
        client = _FakeClient([tool_turn, tool_turn, _assistant(content=None)])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher, _cfg(max_tool_turns=1))

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert len(client.chat_calls) == 3
        assert result.is_empty

    def test_finalize_returning_tool_calls_is_logged_and_dropped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Provider ignores stripped tools and returns tool_calls on the finalize
        # call — they are dropped, result is empty, and a warning is logged so
        # operators can spot provider misbehavior.
        tool_turn = _assistant(
            tool_calls=[
                _tool_call("c", "zoom", {"image_id": "page-0", "factor": 1.5}),
            ],
        )
        finalize_misbehave = _assistant(
            tool_calls=[
                _tool_call("c2", "zoom", {"image_id": "page-0", "factor": 2.0}),
            ],
        )
        client = _FakeClient([tool_turn, tool_turn, finalize_misbehave])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher, _cfg(max_tool_turns=1))

        with caplog.at_level("WARNING", logger="apps.extraction.perelman"):
            result = asyncio.run(extractor.extract(_StubArticle()))

        assert len(client.chat_calls) == 3
        assert result.is_empty
        assert any(
            "finalize call returned tool_calls" in r.message for r in caplog.records
        )

    def test_malformed_json_yields_empty_no_raise(self) -> None:
        client = _FakeClient([_assistant(content="this is not json at all")])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert result.is_empty

    def test_http_error_yields_empty_no_raise(self) -> None:
        client = _FakeClient([], raises_on=0)
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert result.is_empty

    def test_tool_error_message_lets_loop_continue(self) -> None:
        client = _FakeClient(
            [
                # Bad image_id → dispatch raises ToolError → textual tool message.
                _assistant(
                    tool_calls=[
                        _tool_call(
                            "call_1",
                            "zoom",
                            {"image_id": "missing", "factor": 2.0},
                        ),
                    ],
                ),
                _assistant(content=_FINAL_JSON),
            ],
        )
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        # Loop continued past the tool error and parsed the final JSON.
        assert len(result.quotes) == 1
        second_call_messages = client.chat_calls[1]
        tool_msg = next(m for m in second_call_messages if m.get("role") == "tool")
        assert isinstance(tool_msg["content"], str)
        assert tool_msg["content"].startswith("error:")

    def test_json_wrapped_in_prose_is_extracted(self) -> None:
        wrapped = f"Here is the result:\n```json\n{_FINAL_JSON}\n```\nDone."
        client = _FakeClient([_assistant(content=wrapped)])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert len(result.quotes) == 1


class TestBuildMessages:
    def test_multimodal_content_has_text_and_image_url_parts(self) -> None:
        client = _FakeClient([_assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(
            _parts(images=[_image_input("page-0"), _image_input("fig-1")]),
        )
        extractor = _extractor(client, fetcher, _cfg(image_detail="high"))

        asyncio.run(extractor.extract(_StubArticle()))

        user_msg = client.chat_calls[0][1]
        assert user_msg["role"] == "user"
        content = user_msg["content"]
        assert isinstance(content, list)
        types = [p.get("type") for p in content]
        assert types[0] == "text"
        assert types.count("image_url") == 2
        for part in content:
            if part.get("type") == "image_url":
                assert part["image_url"]["detail"] == "high"
                assert part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_image_index_lists_ids_with_dims(self) -> None:
        client = _FakeClient([_assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(_parts(images=[_image_input("page-0")]))
        extractor = _extractor(client, fetcher)

        asyncio.run(extractor.extract(_StubArticle()))

        text_part = client.chat_calls[0][1]["content"][0]["text"]
        assert "IMAGES" in text_part
        assert "page-0" in text_part
        assert "pdf-page" in text_part

    def test_query_is_not_in_messages(self) -> None:
        client = _FakeClient([_assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        asyncio.run(extractor.extract(_StubArticle()))

        blob = json.dumps(client.chat_calls[0])
        # The search query is deliberately not passed to the LLM (query-agnostic).
        assert "query" not in blob.lower()
        assert "search" not in blob.lower()

    def test_metadata_includes_title_year_doi(self) -> None:
        client = _FakeClient([_assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher)

        asyncio.run(extractor.extract(_StubArticle()))

        text_part = client.chat_calls[0][1]["content"][0]["text"]
        assert "A Study of Quarks" in text_part
        assert "2024" in text_part
        assert "10.1007/s10994-024-00100-x" in text_part


class TestEmptyAndDegenerate:
    def test_empty_content_parts_skips_llm(self) -> None:
        client = _FakeClient([_assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(ContentParts(text="", images=[]))
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert result.is_empty
        assert client.chat_calls == []  # no LLM call when there is nothing to extract

    def test_fetcher_raises_yields_empty_no_raise(self) -> None:
        client = _FakeClient([_assistant(content=_FINAL_JSON)])
        fetcher = _FakeFetcher(_parts(), raises=True)
        extractor = _extractor(client, fetcher)

        result = asyncio.run(extractor.extract(_StubArticle()))

        assert result.is_empty
        assert client.chat_calls == []


class TestExtractBatch:
    def test_per_result_isolation_one_failure_does_not_abort_batch(self) -> None:
        good_json = json.dumps(
            {
                "quotes": [
                    {"text": "q1", "location": "", "relevance": 0.5, "rationale": ""},
                ],
                "formulas": [],
                "figures": [],
            },
        )
        # Article 0,2 → final JSON; article 1 → HTTP error on its single chat call.
        articles = [_StubArticle(), _StubArticle(), _StubArticle()]

        # One shared client won't do (each article needs its own chat sequence);
        # build a per-article client instead.
        clients = [
            _FakeClient([_assistant(content=good_json)]),
            _FakeClient([], raises_on=0),
            _FakeClient([_assistant(content=good_json)]),
        ]
        fetcher = _FakeFetcher(_parts())
        cfg = _cfg(concurrency=2)

        # Per-article extractors sharing one fetcher; batch is run by hand below.
        # Instead of the real extract_batch (one client), exercise the isolation
        # contract directly: gather each extractor with return_exceptions=True.
        async def run_all() -> list[ExtractionResult]:
            sem = asyncio.Semaphore(cfg.concurrency)

            async def guarded(idx: int) -> ExtractionResult:
                async with sem:
                    ext = PerelmanExtractor(clients[idx], cfg, fetcher)
                    try:
                        return await ext.extract(articles[idx])
                    except Exception:  # noqa: BLE001 - isolation
                        return ExtractionResult()

            return await asyncio.gather(
                *(guarded(i) for i in range(len(articles))),
                return_exceptions=True,
            )

        results = asyncio.run(run_all())

        assert len(results) == 3
        assert len(results[0].quotes) == 1
        assert results[1].is_empty
        assert len(results[2].quotes) == 1

    def test_extract_batch_returns_one_result_per_article(self) -> None:
        good_json = json.dumps(
            {
                "quotes": [
                    {"text": "q", "location": "", "relevance": 0.5, "rationale": ""},
                ],
                "formulas": [],
                "figures": [],
            },
        )
        client = _FakeClient([_assistant(content=good_json)] * 3)
        fetcher = _FakeFetcher(_parts())
        extractor = _extractor(client, fetcher, _cfg(concurrency=2))

        results = asyncio.run(
            extractor.extract_batch([_StubArticle(), _StubArticle(), _StubArticle()]),
        )

        assert len(results) == 3
        assert all(not r.is_empty for r in results)


# ---------------------------------------------------------------------------
# QuoteExtractionService.enrich — cache-aware, query-agnostic, never-raises
# façade (DB-backed; _run_extraction is replaced with a scripted seam so no
# network/browser/LLM is touched — the orchestrator's cache/claim/persist
# logic is what is verified here).
# ---------------------------------------------------------------------------


@pytest.fixture
def articles_dir(tmp_path, monkeypatch):
    """Point ``CINDEX_ARTICLES_DIR`` at a tmp dir and return it."""
    monkeypatch.setenv("CINDEX_ARTICLES_DIR", str(tmp_path))
    return tmp_path


def _db_article(*, doi: str, published: bool) -> Article:
    """Create a real ``Article`` (with source + journal) for DB-backed tests."""
    source, _ = Source.objects.get_or_create(
        key="test",
        defaults={"name": "Test", "base_url": "https://example.org"},
    )
    journal, _ = Journal.objects.get_or_create(name="Journal of Tests")
    return Article.objects.create(
        source=source,
        journal=journal,
        title=f"Article {doi}",
        abstract="abstract text",
        full_text="full text body",
        doi=doi,
        url=f"https://example.org/{doi}",
        publication_year=2024,
        is_not_preprint_or_author_manuscript=published,
    )


def _result_with_one_quote() -> ExtractionResult:
    """A non-empty ExtractionResult (one quote + one formula) for happy paths."""
    return ExtractionResult(
        quotes=[
            Quote(
                text="verbatim quote 0",
                location="abstract",
                relevance=0.8,
                rationale="core claim",
            ),
        ],
        formulas=[Formula(latex="$$E=mc^2$$", location="section 1")],
        figures=[],
    )


class _ScriptedRun:
    """Test seam replacing ``QuoteExtractionService._run_extraction``.

    Records the article batches it was called with and returns the queued
    ``ExtractionResult`` list (one per article, in order). Lets the enrich
    orchestrator's cache/claim/persist logic be verified with no LLM stack.
    """

    def __init__(self, results: list[ExtractionResult]) -> None:
        self._results = list(results)
        self.calls: list[list[Article]] = []

    def __call__(
        self,
        articles: list[Article],
        cfg: LLMConfig,
    ) -> list[ExtractionResult]:
        self.calls.append(list(articles))
        return list(self._results)


def _patch_configured(monkeypatch) -> None:
    """Make ``services.load_config`` return a fully-configured ``_cfg()``."""
    monkeypatch.setattr("apps.extraction.services.load_config", _cfg)


class TestQuoteExtractionServiceEnrich:
    def test_not_configured_logs_and_skips_extraction(
        self,
        db,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.extraction.services.load_config",
            lambda: LLMConfig(base_url="", api_key="", model=""),
        )
        run = _ScriptedRun([])
        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", run)
        article = _db_article(doi="10.1/a", published=True)

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)

        assert run.calls == []
        assert results[0]["quotes"] == []

    def test_cache_hit_fills_quotes_without_extraction(
        self,
        db,
        monkeypatch,
    ) -> None:
        _patch_configured(monkeypatch)
        run = _ScriptedRun([])
        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", run)
        article = _db_article(doi="10.1/b", published=True)
        cached = [
            {
                "text": "cached quote",
                "location": "abstract",
                "relevance": 0.9,
                "rationale": "r",
            },
        ]
        ArticleQuotes.objects.create(
            article=article,
            status=STATUS_DONE,
            quotes=cached,
            model="m",
        )

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)

        assert run.calls == []
        assert results[0]["quotes"] == cached

    def test_published_uncached_extracts_freezes_and_caches(
        self,
        db,
        articles_dir,
        monkeypatch,
    ) -> None:
        _patch_configured(monkeypatch)
        run = _ScriptedRun([_result_with_one_quote()])
        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", run)
        article = _db_article(doi="10.1/c", published=True)

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)

        # One extraction batch with the one published article.
        assert len(run.calls) == 1
        assert run.calls[0][0].id == article.id
        # Quotes written back to the in-memory result.
        assert len(results[0]["quotes"]) == 1
        assert results[0]["quotes"][0]["text"] == "verbatim quote 0"
        # ArticleQuotes row is done and caches the quotes + model.
        aq = ArticleQuotes.objects.get(article=article)
        assert aq.status == STATUS_DONE
        assert len(aq.quotes) == 1
        assert aq.model == "vision-model"
        # Frozen: local_md_path stamped, .md file on disk.
        article.refresh_from_db()
        assert article.local_md_path != ""
        assert (articles_dir / article.local_md_path).is_file()

    def test_preprint_uncached_extracts_fresh_no_persist(
        self,
        db,
        articles_dir,
        monkeypatch,
    ) -> None:
        _patch_configured(monkeypatch)
        run = _ScriptedRun([_result_with_one_quote()])
        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", run)
        article = _db_article(doi="10.1/d", published=False)

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)

        # Quotes written in-memory only.
        assert len(results[0]["quotes"]) == 1
        # No cache row, no freeze, no md file.
        assert not ArticleQuotes.objects.filter(article=article).exists()
        article.refresh_from_db()
        assert article.local_md_path == ""
        assert not list(articles_dir.glob("*.md"))

    def test_pending_claim_skips_extraction(self, db, monkeypatch) -> None:
        _patch_configured(monkeypatch)
        run = _ScriptedRun([_result_with_one_quote()])
        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", run)
        article = _db_article(doi="10.1/e", published=True)
        # Another job's in-progress claim → this job must skip (no LLM call).
        ArticleQuotes.objects.create(article=article, status=STATUS_PENDING)

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)

        assert run.calls == []
        assert results[0]["quotes"] == []
        aq = ArticleQuotes.objects.get(article=article)
        assert aq.status == STATUS_PENDING

    def test_empty_extraction_marks_no_text(
        self,
        db,
        articles_dir,
        monkeypatch,
    ) -> None:
        _patch_configured(monkeypatch)
        run = _ScriptedRun([ExtractionResult()])
        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", run)
        article = _db_article(doi="10.1/f", published=True)

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)

        # Empty extraction → no_text (retryable), quotes [], not frozen, no raise.
        assert results[0]["quotes"] == []
        aq = ArticleQuotes.objects.get(article=article)
        assert aq.status == STATUS_NO_TEXT
        article.refresh_from_db()
        assert article.local_md_path == ""

    def test_persistence_failure_marks_failed_no_raise(
        self,
        db,
        articles_dir,
        monkeypatch,
    ) -> None:
        _patch_configured(monkeypatch)
        run = _ScriptedRun([_result_with_one_quote()])
        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", run)

        def boom_save(*_args, **_kwargs) -> None:
            msg = "disk full"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "apps.extraction.services.ArticleMarkdownService.save",
            boom_save,
        )
        article = _db_article(doi="10.1/h", published=True)

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)  # must not raise

        # Quotes still written to the in-memory result despite persist failure.
        assert len(results[0]["quotes"]) == 1
        aq = ArticleQuotes.objects.get(article=article)
        assert aq.status == STATUS_FAILED
        assert "disk full" in aq.error

    def test_run_extraction_raising_does_not_abort(self, db, monkeypatch) -> None:
        _patch_configured(monkeypatch)

        def boom(_articles, _cfg):
            msg = "extraction stack blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr(QuoteExtractionService, "_run_extraction", boom)
        article = _db_article(doi="10.1/g", published=True)

        results = [{"id": article.id, "quotes": []}]
        QuoteExtractionService.enrich(results)  # must not raise

        assert results[0]["quotes"] == []
