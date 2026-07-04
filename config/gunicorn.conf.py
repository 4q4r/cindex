"""Gunicorn configuration for ASGI deployment."""

from __future__ import annotations

import multiprocessing
import os


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable with a safe fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
backlog = _env_int("GUNICORN_BACKLOG", 2048)
workers = _env_int("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count()))
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "uvicorn_worker.UvicornWorker")

preload_app = True

max_requests = _env_int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = _env_int("GUNICORN_MAX_REQUESTS_JITTER", 100)

timeout = _env_int("GUNICORN_TIMEOUT", 120)
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _env_int("GUNICORN_KEEPALIVE", 5)

limit_request_line = _env_int("GUNICORN_LIMIT_REQUEST_LINE", 8190)
limit_request_fields = _env_int("GUNICORN_LIMIT_REQUEST_FIELDS", 100)
limit_request_field_size = _env_int("GUNICORN_LIMIT_REQUEST_FIELD_SIZE", 8190)

loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
accesslog = "-"
errorlog = "-"


def post_fork(server, worker) -> None:
    """Close inherited DB connections after fork to avoid shared sockets."""
    try:
        from django.db import connections
    except ImportError:
        return
    connections.close_all()
