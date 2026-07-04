from __future__ import annotations

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse


async def healthcheck_view(request):
    """Report database and cache health without blocking the event loop."""
    if request.method != "GET":
        return JsonResponse({"detail": "Method not allowed."}, status=405)

    @sync_to_async
    def check_db() -> str:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return "ok"

    @sync_to_async
    def check_cache() -> str:
        cache.set("healthcheck", "ok", timeout=3)
        value = cache.get("healthcheck")
        return "ok" if value == "ok" else "error"

    db_status = "ok"
    cache_status = "ok"

    try:
        db_status = await check_db()
    except (ValueError, RuntimeError, ConnectionError) as exc:
        db_status = f"error: {exc}"

    try:
        cache_status = await check_cache()
    except (ValueError, RuntimeError, ConnectionError) as exc:
        cache_status = f"error: {exc}"

    payload = {"status": "healthy", "db": db_status, "cache": cache_status}
    if db_status != "ok" or cache_status != "ok":
        payload["status"] = "unhealthy"
        return JsonResponse(payload, status=503)
    return JsonResponse(payload, status=200)
