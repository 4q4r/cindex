from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("articles", "0002_source_circuit_open_until_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="article",
            name="peer_review_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="article",
            name="indexing_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="article",
            name="doi_and_card_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="article",
            name="not_preprint_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="article",
            name="eligibility_confidence",
            field=models.FloatField(default=0.0),
        ),
    ]
