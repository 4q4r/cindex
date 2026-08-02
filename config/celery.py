"""Celery application bootstrap and signal handlers for CIndex."""

from __future__ import annotations

import logging
import logging.config
import os
from typing import Any

import structlog
from celery import Celery
from celery.signals import setup_logging, task_postrun, task_prerun, worker_ready
from django.conf import settings
from django.db import close_old_connections
from django_structlog.celery.steps import DjangoStructLogInitStep

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("cindex")
if app.steps is None:
    msg = "Celery app.steps must not be None before registering worker steps"
    raise RuntimeError(msg)
app.steps["worker"].add(DjangoStructLogInitStep)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Recycle prefork children after a bounded number of tasks so any DB
# connections they hold (or leak) are released back to postgres. With
# CONN_MAX_AGE=0 + per-task close_old_connections (see handlers below) this
# is belt-and-suspenders against connection accumulation under ASGI/celery.
app.conf.worker_max_tasks_per_child = 200


@task_prerun.connect
def _close_old_connections_prerun(**_: object) -> None:
    """Close stale DB connections before a celery task runs.

    Django's request-цикл normally calls ``close_old_connections`` per
    request, but celery prefork children have no such цикл — without this
    signal a child holds its Django DB connection for the whole process
    lifetime. Under CONN_MAX_AGE=0 this releases the connection back to
    postgres after every task, preventing the pool exhaustion that caused
    ``FATAL: sorry, too many clients already`` → HTTP 500 on the poll
    endpoint. (Canonical django+celery pattern.)
    """
    close_old_connections()


@task_postrun.connect
def _close_old_connections_postrun(**_: object) -> None:
    """Close stale DB connections after a celery task finishes."""
    close_old_connections()


def _build_beat_schedule() -> dict[str, dict[str, object]]:
    """Build the Celery beat schedule from application settings."""
    schedule: dict[str, dict[str, object]] = {}

    local_import_interval_seconds = int(
        getattr(
            getattr(settings, "APP", None),
            "local_import_scan_interval_seconds",
            0,
        ),
    )
    if local_import_interval_seconds > 0:
        schedule["local-import-refresh"] = {
            "task": "apps.ingestion.tasks.scan_local_imports",
            "schedule": local_import_interval_seconds,
        }

    exa_quota_sync_interval_seconds = int(
        getattr(getattr(settings, "APP", None), "exa_quota_sync_interval_seconds", 0),
    )
    if exa_quota_sync_interval_seconds > 0:
        schedule["exa-quota-sync"] = {
            "task": "apps.ingestion.tasks.sync_exa_quota_snapshots",
            "schedule": exa_quota_sync_interval_seconds,
        }

    return schedule


app.conf.beat_schedule = _build_beat_schedule()


@setup_logging.connect
def _configure_structlog_for_celery(
    loglevel: int,  # noqa: ARG001  # celery signal signature
    logfile: str,  # noqa: ARG001  # celery signal signature
    format: str,  # noqa: ARG001, A002  # celery signal signature
    colorize: bool,  # noqa: ARG001, FBT001  # celery signal signature
    **kwargs: Any,  # noqa: ARG001, ANN401  # celery signal signature
) -> None:
    """Configure structlog in Celery worker processes."""
    for logger_name in logging.root.manager.loggerDict:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()

    logging.config.dictConfig(settings.LOGGING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


@worker_ready.connect
def resume_interrupted_search_jobs(**_: object) -> None:
    """Requeue interrupted search jobs when a Celery worker becomes ready."""
    if os.getenv("CINDEX_RESUME_SEARCH_JOBS", "").lower() not in {"1", "true", "yes"}:
        return
    # lazy import: avoid circular import / app registry not ready
    from apps.search.recovery import resume_running_search_jobs  # noqa: PLC0415

    resume_running_search_jobs()
