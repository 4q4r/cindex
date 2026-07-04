"""Add server-side filter and sort parameters to SearchJob."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add eligibility/year-filter and sort fields to SearchJob."""

    dependencies = [
        ("search", "0005_remove_searchwaitstat_seeded_from_history_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="searchjob",
            name="peer_reviewed_only",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="searchjob",
            name="indexed_only",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="searchjob",
            name="exclude_preprints",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="searchjob",
            name="year_from",
            field=models.IntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="searchjob",
            name="year_to",
            field=models.IntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="searchjob",
            name="sort_by",
            field=models.CharField(default="relevance", max_length=16),
        ),
    ]
