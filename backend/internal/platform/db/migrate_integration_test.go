package db_test

import (
	"context"
	"testing"
	"time"

	"github.com/4q4r/cindex/backend/internal/platform/db"
	"github.com/4q4r/cindex/backend/migrations"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"
)

// TestMigrateAppliesFullSchema runs the embedded tern migrations against a
// fresh PostgreSQL container and asserts every expected table exists.
// Integration-only: skipped when the Docker daemon is unavailable.
func TestMigrateAppliesFullSchema(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	container, err := postgres.Run(ctx,
		"postgres:17-alpine",
		postgres.WithDatabase("cindex"),
		postgres.WithUsername("cindex"),
		postgres.WithPassword("cindex"),
		testcontainers.WithWaitStrategy(
			wait.ForLog("database system is ready to accept connections").
				WithOccurrence(2).WithStartupTimeout(60*time.Second),
		),
	)
	if err != nil {
		t.Skipf("docker unavailable, skipping integration test: %v", err)
	}
	t.Cleanup(func() { _ = container.Terminate(ctx) })

	url, err := container.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		t.Fatal(err)
	}

	pool, err := db.Open(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	if err := db.Migrate(ctx, pool, migrations.FS); err != nil {
		t.Fatal(err)
	}

	// Idempotent: a second run must be a no-op.
	if err := db.Migrate(ctx, pool, migrations.FS); err != nil {
		t.Fatalf("second migrate run: %v", err)
	}

	want := []string{
		"articles_source",
		"articles_journal",
		"articles_article",
		"articles_author",
		"articles_articleauthor",
		"articles_identifier",
		"search_searchjob",
		"search_searchwaitstat",
		"extraction_articlequotes",
		"ingestion_ingestionrun",
		"ingestion_localimportfile",
		"ingestion_exaapikeyquota",
		"schema_version",
	}
	for _, table := range want {
		var exists bool
		err := pool.QueryRow(ctx,
			`SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)`,
			table,
		).Scan(&exists)
		if err != nil {
			t.Fatalf("check table %s: %v", table, err)
		}
		if !exists {
			t.Errorf("table %q missing after migration", table)
		}
	}

	// Column-level parity spot checks against the Django schema.
	checks := []struct {
		table, column string
	}{
		{"articles_article", "search_vector"},
		{"articles_article", "peer_review_confidence"},
		{"articles_article", "is_eligible"},
		{"articles_article", "local_md_path"},
		{"extraction_articlequotes", "tldr"},
		{"extraction_articlequotes", "quotes"},
		{"search_searchjob", "id"},
		{"ingestion_exaapikeyquota", "usage_total_cost_usd"},
	}
	for _, c := range checks {
		var exists bool
		err := pool.QueryRow(ctx,
			`SELECT EXISTS (
				SELECT 1 FROM information_schema.columns
				WHERE table_name = $1 AND column_name = $2
			)`,
			c.table, c.column,
		).Scan(&exists)
		if err != nil {
			t.Fatalf("check column %s.%s: %v", c.table, c.column, err)
		}
		if !exists {
			t.Errorf("column %s.%s missing after migration", c.table, c.column)
		}
	}

	// JSONB types for the JSON columns.
	var dataType string
	err = pool.QueryRow(ctx,
		`SELECT data_type FROM information_schema.columns
		 WHERE table_name = 'search_searchjob' AND column_name = 'results'`,
	).Scan(&dataType)
	if err != nil {
		t.Fatal(err)
	}
	if dataType != "jsonb" {
		t.Errorf("search_searchjob.results type = %q, want jsonb", dataType)
	}
}
