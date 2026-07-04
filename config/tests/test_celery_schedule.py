from __future__ import annotations

from django.conf import settings

from config.celery import _build_beat_schedule


def test_nightly_ingestion_schedule_is_disabled_when_interval_is_zero(
    monkeypatch: object,
) -> None:
    """A zero interval must disable nightly ingestion from celery beat."""
    monkeypatch.setattr(
        settings.APP, "nightly_ingestion_interval_seconds", 0, raising=False
    )
    monkeypatch.setattr(
        settings.APP, "local_import_scan_interval_seconds", 0, raising=False
    )

    assert _build_beat_schedule() == {}


def test_nightly_ingestion_schedule_is_enabled_when_interval_is_positive(
    monkeypatch: object,
) -> None:
    """A positive interval must enable nightly ingestion from celery beat."""
    monkeypatch.setattr(
        settings.APP, "nightly_ingestion_interval_seconds", 3600, raising=False
    )
    monkeypatch.setattr(
        settings.APP, "local_import_scan_interval_seconds", 0, raising=False
    )

    assert _build_beat_schedule() == {
        "nightly-refresh": {
            "task": "apps.ingestion.tasks.nightly_ingestion",
            "schedule": 3600,
        }
    }


def test_local_import_schedule_is_enabled_when_interval_is_positive(
    monkeypatch: object,
) -> None:
    """A positive local import interval must enable the local scan schedule."""
    monkeypatch.setattr(
        settings.APP, "nightly_ingestion_interval_seconds", 0, raising=False
    )
    monkeypatch.setattr(
        settings.APP, "local_import_scan_interval_seconds", 30, raising=False
    )

    assert _build_beat_schedule() == {
        "local-import-refresh": {
            "task": "apps.ingestion.tasks.scan_local_imports",
            "schedule": 30,
        }
    }


def test_exa_quota_sync_schedule_is_enabled_when_interval_is_positive(
    monkeypatch: object,
) -> None:
    """A positive Exa interval must enable quota sync in celery beat."""
    monkeypatch.setattr(
        settings.APP, "nightly_ingestion_interval_seconds", 0, raising=False
    )
    monkeypatch.setattr(
        settings.APP, "local_import_scan_interval_seconds", 0, raising=False
    )
    monkeypatch.setattr(
        settings.APP, "exa_quota_sync_interval_seconds", 900, raising=False
    )

    assert _build_beat_schedule() == {
        "exa-quota-sync": {
            "task": "apps.ingestion.tasks.sync_exa_quota_snapshots",
            "schedule": 900,
        }
    }
