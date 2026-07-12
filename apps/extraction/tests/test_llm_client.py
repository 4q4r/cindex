"""Unit tests for :class:`OpenAICompatibleClient`.

Mirrors the canonical Exa transport tests in
``apps/ingestion/tests/test_exa_connector.py``: ``aiohttp.ClientSession`` is
monkeypatched with a fake that records construction + ``post`` kwargs so we can
pin the contract — ``trust_env=True`` (the AST guard is satisfied), Bearer auth
header, multimodal/tool fields forwarded, the full assistant ``message`` dict
returned, and terminal vs transient failures handled the same way as the Exa
connector. No real network is touched.
"""

import asyncio
import json
import time
from typing import Self

import aiohttp
import pytest

from apps.extraction.config import LLMConfig, LLMNotConfiguredError
from apps.extraction.llm_client import OpenAICompatibleClient
from apps.ingestion.connectors.base import ConnectorFetchError


class _FakeResponse:
    """Mimics aiohttp's response async context manager."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: str = "",
        enter_exc: BaseException | None = None,
        text_exc: BaseException | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._enter_exc = enter_exc
        self._text_exc = text_exc

    async def __aenter__(self) -> Self:
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        if self._text_exc is not None:
            raise self._text_exc
        return self._body


class _FakeAiohttp:
    """Replaces ``aiohttp.ClientSession`` so calls can be asserted offline."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.init_kwargs: dict[str, object] = {}
        self.post_kwargs: dict[str, object] = {}

    def client_session(self, *args: object, **kwargs: object) -> "_FakeAiohttp":
        self.init_kwargs = kwargs
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.post_kwargs = {"url": url, **kwargs}
        return self._responses.pop(0)


def _cfg(**overrides: object) -> LLMConfig:
    base: dict[str, object] = {
        "base_url": "https://llm.example.com/v1",
        "api_key": "secret",
        "model": "vision-model",
    }
    base.update(overrides)
    return LLMConfig(**base)  # type: ignore[arg-type]


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_FakeResponse],
) -> _FakeAiohttp:
    from apps.extraction import llm_client

    fake = _FakeAiohttp(responses)
    monkeypatch.setattr(llm_client.aiohttp, "ClientSession", fake.client_session)
    return fake


class TestOpenAICompatibleClient:
    """``OpenAICompatibleClient.chat`` contract tests."""

    def test_missing_api_key_raises_loudly(self) -> None:
        with pytest.raises(LLMNotConfiguredError):
            OpenAICompatibleClient(_cfg(api_key=""))

    def test_session_uses_trust_env_and_bearer_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = json.dumps(
            {"choices": [{"message": {"content": "ok", "tool_calls": None}}]},
        )
        fake = _install_fake(monkeypatch, [_FakeResponse(status=200, body=body)])
        client = OpenAICompatibleClient(_cfg())

        message = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        assert message == {"content": "ok", "tool_calls": None}
        assert fake.init_kwargs["trust_env"] is True
        assert fake.post_kwargs["url"] == "https://llm.example.com/v1/chat/completions"
        headers = fake.post_kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret"
        assert headers["Content-Type"] == "application/json"
        assert "cindex" not in str(headers).lower()

    def test_model_and_temperature_in_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = json.dumps({"choices": [{"message": {"content": "x"}}]})
        fake = _install_fake(monkeypatch, [_FakeResponse(status=200, body=body)])
        client = OpenAICompatibleClient(_cfg(temperature=0.7))

        asyncio.run(client.chat([{"role": "user", "content": "hi"}], temperature=0.1))

        sent = fake.post_kwargs["json"]
        assert sent["model"] == "vision-model"
        assert sent["temperature"] == 0.1
        assert sent["messages"] == [{"role": "user", "content": "hi"}]
        # response_format / tools / tool_choice absent when not passed
        assert "response_format" not in sent
        assert "tools" not in sent
        assert "tool_choice" not in sent

    def test_extra_body_merged_and_override_wins(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = json.dumps({"choices": [{"message": {"content": "x"}}]})
        fake = _install_fake(monkeypatch, [_FakeResponse(status=200, body=body)])
        cfg = _cfg(extra_body={"thinking": {"budget": 1}, "shared": "cfg"})
        client = OpenAICompatibleClient(cfg)

        asyncio.run(
            client.chat(
                [{"role": "user", "content": "hi"}],
                extra_body_override={"thinking": {"budget": 2}, "only": "here"},
            ),
        )

        sent = fake.post_kwargs["json"]
        assert sent["shared"] == "cfg"
        assert sent["only"] == "here"
        assert sent["thinking"] == {"budget": 2}

    def test_tools_and_tool_choice_and_response_format_forwarded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = json.dumps({"choices": [{"message": {"content": "{}"}}]})
        fake = _install_fake(monkeypatch, [_FakeResponse(status=200, body=body)])
        client = OpenAICompatibleClient(_cfg())
        tools = [{"type": "function", "function": {"name": "zoom"}}]

        asyncio.run(
            client.chat(
                [{"role": "user", "content": "hi"}],
                tools=tools,
                tool_choice="auto",
                response_format={"type": "json_object"},
            ),
        )

        sent = fake.post_kwargs["json"]
        assert sent["tools"] == tools
        assert sent["tool_choice"] == "auto"
        assert sent["response_format"] == {"type": "json_object"}

    def test_multimodal_messages_passed_through_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = json.dumps({"choices": [{"message": {"content": "x"}}]})
        fake = _install_fake(monkeypatch, [_FakeResponse(status=200, body=body)])
        client = OpenAICompatibleClient(_cfg())
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AAAA",
                            "detail": "high",
                        },
                    },
                ],
            },
        ]

        asyncio.run(client.chat(messages))

        # The client never introspects content — it is forwarded as-is.
        assert fake.post_kwargs["json"]["messages"] is messages

    def test_returns_full_message_with_tool_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        message = {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "zoom", "arguments": '{"image_id":"p0"}'},
                },
            ],
        }
        body = json.dumps({"choices": [{"message": message}]})
        _install_fake(monkeypatch, [_FakeResponse(status=200, body=body)])
        client = OpenAICompatibleClient(_cfg())

        result = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        assert result == message
        assert result["tool_calls"][0]["function"]["name"] == "zoom"

    def test_http_error_raises_connector_fetch_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake(monkeypatch, [_FakeResponse(status=429, body="rate limited")])
        client = OpenAICompatibleClient(_cfg())

        with pytest.raises(ConnectorFetchError, match="HTTP 429"):
            asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    def test_invalid_json_raises_connector_fetch_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake(
            monkeypatch,
            [_FakeResponse(status=200, body="<!doctype html>")],
        )
        client = OpenAICompatibleClient(_cfg())

        with pytest.raises(ConnectorFetchError, match="invalid JSON"):
            asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    def test_missing_choices_raises_connector_fetch_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake(
            monkeypatch,
            [_FakeResponse(status=200, body=json.dumps({"id": "x"}))],
        )
        client = OpenAICompatibleClient(_cfg())

        with pytest.raises(ConnectorFetchError, match="missing choices"):
            asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    def test_transient_client_error_is_retried_then_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]})
        responses = [
            _FakeResponse(enter_exc=aiohttp.ClientConnectionError("boom")),
            _FakeResponse(status=200, body=body),
        ]
        fake = _install_fake(monkeypatch, responses)
        client = OpenAICompatibleClient(_cfg())

        async def _no_sleep(_t: float) -> None:  # async to patch sleep
            return None

        monkeypatch.setattr(
            "apps.extraction.llm_client.asyncio.sleep",
            _no_sleep,
        )

        message = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        assert message["content"] == "ok"
        assert len(fake.post_kwargs) > 0

    def test_transient_client_error_exhausts_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake(
            monkeypatch,
            [
                _FakeResponse(enter_exc=aiohttp.ClientConnectionError("boom")),
                _FakeResponse(enter_exc=aiohttp.ClientConnectionError("boom")),
                _FakeResponse(enter_exc=aiohttp.ClientConnectionError("boom")),
            ],
        )
        client = OpenAICompatibleClient(_cfg())

        async def _no_sleep(_t: float) -> None:  # async to patch sleep
            return None

        monkeypatch.setattr(
            "apps.extraction.llm_client.asyncio.sleep",
            _no_sleep,
        )

        with pytest.raises(ConnectorFetchError, match="transient failure"):
            asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    def test_min_request_interval_spaces_consecutive_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two chat calls are spaced at least ``min_request_interval`` apart.

        ``asyncio.sleep`` is recorded; the first call sleeps ~interval (gated
        against an uninitialised timestamp it would not, but monotonic starts
        large so the first call fires immediately and the second waits the
        full interval). Real elapsed time is asserted to be >= interval.
        """
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]})
        _install_fake(
            monkeypatch,
            [
                _FakeResponse(status=200, body=body),
                _FakeResponse(status=200, body=body),
            ],
        )
        client = OpenAICompatibleClient(_cfg(min_request_interval=0.05))

        async def _run() -> float:
            start = time.monotonic()
            await client.chat([{"role": "user", "content": "a"}])
            await client.chat([{"role": "user", "content": "b"}])
            return time.monotonic() - start

        elapsed = asyncio.run(_run())
        assert elapsed >= 0.05

    def test_zero_min_request_interval_is_noop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With ``min_request_interval=0`` the gate never sleeps."""
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]})
        fake = _install_fake(monkeypatch, [_FakeResponse(status=200, body=body)])
        client = OpenAICompatibleClient(_cfg(min_request_interval=0.0))

        slept: list[float] = []

        async def _spy_sleep(t: float) -> None:
            slept.append(t)

        monkeypatch.setattr(
            "apps.extraction.llm_client.asyncio.sleep",
            _spy_sleep,
        )

        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        assert fake.post_kwargs["json"]["messages"] == [
            {"role": "user", "content": "hi"},
        ]
        assert slept == []
