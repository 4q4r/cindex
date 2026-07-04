from django.db import migrations


def delete_articles_without_doi(apps, schema_editor):
    Article = apps.get_model("articles", "Article")
    # ArticleAuthor and Identifier cascade on Article deletion.
    # LocalImportFile.article uses SET_NULL, so those records stay with article=NULL.
    Article.objects.filter(doi="").delete()
    Article.objects.exclude(doi__startswith="10.").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("articles", "0006_add_search_vector_and_indexes"),
    ]

    operations = [
        migrations.RunPython(
            delete_articles_without_doi,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
