from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp
from django.utils import timezone

from .models import ExaApiKeyQuota

EXA_TEAM_MANAGEMENT_BASE_URL = "https://admin-api.exa.ai/team-management"


def _api_key() -> str:
    """Return the Exa team-management API key."""
    return os.getenv("EXA_API_KEY", "").strip()


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO datetime into an aware datetime."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_decimal(value: object) -> Decimal | None:
    """Parse a decimal value safely."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, RuntimeError, TypeError):
        return None


def _parse_int(value: object) -> int | None:
    """Parse an integer value safely."""
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _team_management_headers(api_key: str) -> dict[str, str]:
    """Build headers for Exa team-management requests."""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "cindex/1.0",
        "x-api-key": api_key,
    }


def _extract_api_keys(payload: object) -> list[dict[str, object]]:
    """Extract API key records from a team-management response."""
    if isinstance(payload, dict):
        api_keys = payload.get("apiKeys")
        if isinstance(api_keys, list):
            return [item for item in api_keys if isinstance(item, dict)]
        api_key = payload.get("apiKey")
        if isinstance(api_key, dict):
            return [api_key]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


async def _list_exa_api_keys_async(
    api_key: str | None = None,
) -> list[dict[str, object]]:
    """List all Exa API keys visible to the authenticated team."""
    active_key = api_key or _api_key()
    if not active_key:
        return []
    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"{EXA_TEAM_MANAGEMENT_BASE_URL}/api-keys",
            headers=_team_management_headers(active_key),
            timeout=aiohttp.ClientTimeout(total=20.0),
        ) as response,
    ):
        response.raise_for_status()
        data = await response.json()
    return _extract_api_keys(data)


def list_exa_api_keys(api_key: str | None = None) -> list[dict[str, object]]:
    """List all Exa API keys visible to the authenticated team (sync wrapper)."""
    return asyncio.run(_list_exa_api_keys_async(api_key))


async def _get_exa_api_key_usage_async(
    api_key_id: str,
    api_key: str | None = None,
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, object]:
    """Fetch usage analytics for a specific Exa API key."""
    active_key = api_key or _api_key()
    if not active_key:
        raise ValueError("EXA_API_KEY is required")
    now = timezone.now()
    period_end = end_date or now
    period_start = start_date or (period_end - timedelta(days=30))
    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"{EXA_TEAM_MANAGEMENT_BASE_URL}/api-keys/{api_key_id}/usage",
            headers=_team_management_headers(active_key),
            params={
                "start_date": period_start.isoformat(),
                "end_date": period_end.isoformat(),
                "group_by": "day",
            },
            timeout=aiohttp.ClientTimeout(total=20.0),
        ) as response,
    ):
        response.raise_for_status()
        data = await response.json()
    return data if isinstance(data, dict) else {}


def get_exa_api_key_usage(
    api_key_id: str,
    api_key: str | None = None,
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, object]:
    """Fetch usage analytics for a specific Exa API key (sync wrapper)."""
    return asyncio.run(
        _get_exa_api_key_usage_async(
            api_key_id,
            api_key,
            start_date=start_date,
            end_date=end_date,
        ),
    )


def sync_exa_usage_snapshots(
    *, api_key: str | None = None, lookback_days: int = 30,
) -> tuple[int, int]:
    """Sync Exa API key usage snapshots into the local admin dashboard."""
    active_key = api_key or _api_key()
    if not active_key:
        return (0, 0)

    synced = 0
    failed = 0
    now = timezone.now()
    start = now - timedelta(days=lookback_days)

    for key_payload in list_exa_api_keys(api_key=active_key):
        api_key_id = str(key_payload.get("id") or key_payload.get("apiKeyId") or "")
        if not api_key_id:
            failed += 1
            continue
        api_key_name = str(key_payload.get("name") or "")
        rate_limit = _parse_int(key_payload.get("rateLimit"))
        try:
            usage_payload = get_exa_api_key_usage(
                api_key_id,
                api_key=active_key,
                start_date=start,
                end_date=now,
            )
            period = (
                usage_payload.get("period") if isinstance(usage_payload, dict) else {}
            )
            cost_breakdown = usage_payload.get("cost_breakdown") or []
            ExaApiKeyQuota.objects.update_or_create(
                api_key_id=api_key_id,
                defaults={
                    "api_key_name": api_key_name,
                    "rate_limit_per_minute": rate_limit,
                    "usage_total_cost_usd": _parse_decimal(
                        usage_payload.get("total_cost_usd"),
                    ),
                    "usage_breakdown": (
                        cost_breakdown if isinstance(cost_breakdown, list) else []
                    ),
                    "usage_window_start": _parse_datetime(
                        period.get("start") if isinstance(period, dict) else None,
                    ),
                    "usage_window_end": _parse_datetime(
                        period.get("end") if isinstance(period, dict) else None,
                    ),
                    "last_synced_at": timezone.now(),
                    "last_error": "",
                },
            )
            synced += 1
        except (ValueError, RuntimeError, ConnectionError) as exc:
            # pragma: no cover - network dependent
            ExaApiKeyQuota.objects.update_or_create(
                api_key_id=api_key_id,
                defaults={
                    "api_key_name": api_key_name,
                    "rate_limit_per_minute": rate_limit,
                    "usage_total_cost_usd": None,
                    "usage_breakdown": [],
                    "usage_window_start": None,
                    "usage_window_end": None,
                    "last_synced_at": timezone.now(),
                    "last_error": str(exc),
                },
            )
            failed += 1
    return synced, failed
