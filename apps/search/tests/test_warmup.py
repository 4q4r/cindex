from __future__ import annotations

from apps.search import warmup


def test_warmup_search_models_logs_once(monkeypatch) -> None:
    """Warmup should run once per process and only log once."""

    messages: list[str] = []
    monkeypatch.setattr(
        warmup.LOGGER, "info", lambda message, *args: messages.append(message % args)
    )
    warmup.warmup_search_models.cache_clear()

    warmup.warmup_search_models()
    warmup.warmup_search_models()

    assert messages == ["Search warmup completed in 0.00s"]


def test_start_background_warmup_spawns_once(monkeypatch) -> None:
    """Background warmup should only be kicked off once per process."""
    spawned: list[dict[str, object]] = []

    class DummyThread:
        def __init__(self, *, target, name: str, daemon: bool) -> None:  # type: ignore[no-untyped-def]
            spawned.append(
                {
                    "target": target,
                    "name": name,
                    "daemon": daemon,
                }
            )

        def start(self) -> None:
            spawned.append({"started": True})
            target = spawned[0]["target"]
            assert callable(target)
            target()

    monkeypatch.setattr(warmup, "Thread", DummyThread)
    monkeypatch.setattr(warmup, "warmup_search_models", lambda: None)
    warmup.start_background_warmup.cache_clear()

    warmup.start_background_warmup()
    warmup.start_background_warmup()

    assert len(spawned) == 2
    assert spawned[0]["name"] == "search-model-warmup"
    assert spawned[0]["daemon"] is True
