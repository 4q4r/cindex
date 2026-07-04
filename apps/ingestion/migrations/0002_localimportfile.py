from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Create local import file tracking model."""

    dependencies = [
        ("articles", "0004_passage"),
        ("ingestion", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="LocalImportFile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("path", models.CharField(max_length=1024, unique=True)),
                ("sha256", models.CharField(blank=True, max_length=64)),
                ("status", models.CharField(default="pending", max_length=32)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                (
                    "last_seen_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "processed_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "article",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="articles.article",
                    ),
                ),
            ],
        )
    ]
