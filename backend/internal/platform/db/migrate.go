package db

import (
	"context"
	"fmt"
	"io/fs"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/jackc/tern/v2/migrate"
)

// Migrate applies all tern migrations found in fsys in ascending order.
// The schema_version table tracks applied migrations; it is safe to call
// repeatedly and on a populated database.
func Migrate(ctx context.Context, pool *pgxpool.Pool, fsys fs.FS) error {
	poolConn, err := pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire connection for migration: %w", err)
	}
	// Hijack takes the raw pgx connection out of the pool; tern needs the
	// lower-level type to manage transactions and its schema_version table.
	conn := poolConn.Hijack()
	defer func() { _ = conn.Close(ctx) }()

	m, err := migrate.NewMigrator(ctx, conn, "schema_version")
	if err != nil {
		return fmt.Errorf("create migrator: %w", err)
	}
	if err := m.LoadMigrations(fsys); err != nil {
		return fmt.Errorf("load migrations: %w", err)
	}
	if err := m.Migrate(ctx); err != nil {
		return fmt.Errorf("apply migrations: %w", err)
	}
	return nil
}
