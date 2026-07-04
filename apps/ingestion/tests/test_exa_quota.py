from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.ingestion.exa_usage import sync_exa_usage_snapshots
from apps.ingestion.models import ExaApiKeyQuota


class _FakeJsonResponse:
    """Minimal aiohttp-like response for testing."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        """Mimic a successful response."""
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeSession:
    """Fake aiohttp.ClientSession that returns canned responses."""

    def __init__(self, responses: dict):
        self._responses = responses

    def get(self, url, **kwargs):
        response = self._responses.get(str(url))
        if response is None:
            raise AssertionError(f"unexpected URL {url}")
        return response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def test_exa_usage_sync_persists_team_keys(monkeypatch: pytest.MonkeyPatch, db) -> None:
    """Exa usage sync should persist snapshots from official management API."""

    _keys_response = _FakeJsonResponse(
        {
            "apiKeys": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": "Production API Key",
                    "rateLimit": 1000,
                }
            ]
        }
    )
    _usage_response = _FakeJsonResponse(
        {
            "api_key_id": "11111111-1111-1111-1111-111111111111",
            "api_key_name": "Production API Key",
            "team_id": "22222222-2222-2222-2222-222222222222",
            "period": {
                "start": "2025-01-01T00:00:00Z",
                "end": "2025-01-31T23:59:59Z",
            },
            "total_cost_usd": 45.67,
            "cost_breakdown": [
                {
                    "price_id": "price_neural_search",
                    "price_name": "Neural Search",
                    "quantity": 1000,
                    "amount_usd": 30,
                }
            ],
            "metadata": {"generated_at": "2025-02-01T10:30:00Z"},
        }
    )

    async def fake_list_keys(api_key=None):
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Production API Key",
                "rateLimit": 1000,
            }
        ]

    async def fake_get_usage(
        api_key_id,
        api_key=None,
        *,
        start_date=None,
        end_date=None,
    ):
        return {
            "api_key_id": "11111111-1111-1111-1111-111111111111",
            "api_key_name": "Production API Key",
            "team_id": "22222222-2222-2222-2222-222222222222",
            "period": {
                "start": "2025-01-01T00:00:00Z",
                "end": "2025-01-31T23:59:59Z",
            },
            "total_cost_usd": 45.67,
            "cost_breakdown": [
                {
                    "price_id": "price_neural_search",
                    "price_name": "Neural Search",
                    "quantity": 1000,
                    "amount_usd": 30,
                }
            ],
            "metadata": {"generated_at": "2025-02-01T10:30:00Z"},
        }

    monkeypatch.setenv("EXA_API_KEY", "service-key")
    monkeypatch.setattr(
        "apps.ingestion.exa_usage._list_exa_api_keys_async", fake_list_keys
    )
    monkeypatch.setattr(
        "apps.ingestion.exa_usage._get_exa_api_key_usage_async", fake_get_usage
    )

    synced, failed = sync_exa_usage_snapshots(api_key="service-key", lookback_days=30)

    assert synced == 1
    assert failed == 0
    quota = ExaApiKeyQuota.objects.get(
        api_key_id="11111111-1111-1111-1111-111111111111"
    )
    assert quota.api_key_name == "Production API Key"
    assert quota.rate_limit_per_minute == 1000
    assert quota.usage_total_cost_usd == Decimal("45.6700")
    assert quota.usage_breakdown[0]["price_name"] == "Neural Search"
    assert quota.usage_window_start == datetime(2025, 1, 1, tzinfo=UTC)
    assert quota.usage_window_end == datetime(2025, 1, 31, 23, 59, 59, tzinfo=UTC)
