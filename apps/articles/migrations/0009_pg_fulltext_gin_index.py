# Generated for CIndex: PostgreSQL full-text search GIN expression index.

from django.db import migrations


def _create_pg_fulltext_gin_index(apps, schema_editor):
    # Create a GIN expression index on a tsvector built from title, abstract,
    # and full_text. The expression MUST stay byte-identical to the one in
    # SearchService._pg_fts_vector -- PostgreSQL only uses an expression index
    # for ``vector @@ query`` when the indexed expression matches the query
    # expression exactly. SQLite (and any non-PostgreSQL backend) has no GIN
    # support, so this migration is a no-op there.
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS articles_article_fts_gin "
        "ON articles_article USING gin ("
        "to_tsvector('simple', coalesce(title, '') || ' ' || "
        "coalesce(abstract, '') || ' ' || coalesce(full_text, ''))"
        ")",
    )


def _drop_pg_fulltext_gin_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS articles_article_fts_gin")


class Migration(migrations.Migration):
    dependencies = [
        ("articles", "0008_alter_articleauthor_options_alter_article_doi"),
    ]

    operations = [
        migrations.RunPython(
            _create_pg_fulltext_gin_index,
            reverse_code=_drop_pg_fulltext_gin_index,
        ),
    ]
