package repository

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Sources stores connector source rows and their circuit-breaker state.
type Sources struct {
	pool *pgxpool.Pool
}

// NewSources builds the source repository over the given pool.
func NewSources(pool *pgxpool.Pool) *Sources {
	return &Sources{pool: pool}
}

// ListActive returns all enabled sources ordered by key.
func (r *Sources) ListActive(ctx context.Context) ([]domain.Source, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT id, key, name, base_url, active, total_runs, total_successes,
		       total_failures, consecutive_failures, last_checked_at,
		       last_success_at, circuit_open_until, last_error
		FROM articles_source WHERE active = TRUE ORDER BY key`)
	if err != nil {
		return nil, fmt.Errorf("query sources: %w", err)
	}
	defer rows.Close()

	var out []domain.Source
	for rows.Next() {
		var s domain.Source
		if err := rows.Scan(
			&s.ID, &s.Key, &s.Name, &s.BaseURL, &s.Active, &s.TotalRuns,
			&s.TotalSuccesses, &s.TotalFailures, &s.ConsecutiveFailures,
			&s.LastCheckedAt, &s.LastSuccessAt, &s.CircuitOpenUntil, &s.LastError,
		); err != nil {
			return nil, fmt.Errorf("scan source: %w", err)
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// ListAll returns every source (enabled or not) ordered by key, used for
// aggregated source-health statistics.
func (r *Sources) ListAll(ctx context.Context) ([]domain.Source, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT id, key, name, base_url, active, total_runs, total_successes,
		       total_failures, consecutive_failures, last_checked_at,
		       last_success_at, circuit_open_until, last_error
		FROM articles_source ORDER BY key`)
	if err != nil {
		return nil, fmt.Errorf("query all sources: %w", err)
	}
	defer rows.Close()

	var out []domain.Source
	for rows.Next() {
		var s domain.Source
		if err := rows.Scan(
			&s.ID, &s.Key, &s.Name, &s.BaseURL, &s.Active, &s.TotalRuns,
			&s.TotalSuccesses, &s.TotalFailures, &s.ConsecutiveFailures,
			&s.LastCheckedAt, &s.LastSuccessAt, &s.CircuitOpenUntil, &s.LastError,
		); err != nil {
			return nil, fmt.Errorf("scan source: %w", err)
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// GetByKey loads one source by its connector key.
func (r *Sources) GetByKey(ctx context.Context, key string) (*domain.Source, error) {
	var s domain.Source
	err := r.pool.QueryRow(ctx, `
		SELECT id, key, name, base_url, active, total_runs, total_successes,
		       total_failures, consecutive_failures, last_checked_at,
		       last_success_at, circuit_open_until, last_error
		FROM articles_source WHERE key = $1`, key).Scan(
		&s.ID, &s.Key, &s.Name, &s.BaseURL, &s.Active, &s.TotalRuns,
		&s.TotalSuccesses, &s.TotalFailures, &s.ConsecutiveFailures,
		&s.LastCheckedAt, &s.LastSuccessAt, &s.CircuitOpenUntil, &s.LastError,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("get source by key: %w", err)
	}
	return &s, nil
}

// GetByID loads one source by its primary key.
func (r *Sources) GetByID(ctx context.Context, id int64) (*domain.Source, error) {
	var s domain.Source
	err := r.pool.QueryRow(ctx, `
		SELECT id, key, name, base_url, active, total_runs, total_successes,
		       total_failures, consecutive_failures, last_checked_at,
		       last_success_at, circuit_open_until, last_error
		FROM articles_source WHERE id = $1`, id).Scan(
		&s.ID, &s.Key, &s.Name, &s.BaseURL, &s.Active, &s.TotalRuns,
		&s.TotalSuccesses, &s.TotalFailures, &s.ConsecutiveFailures,
		&s.LastCheckedAt, &s.LastSuccessAt, &s.CircuitOpenUntil, &s.LastError,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("get source by id: %w", err)
	}
	return &s, nil
}

// SourceRunOutcome records the result of one connector run and maintains the
// circuit-breaker counters (parity with the Django connector bookkeeping).
type SourceRunOutcome struct {
	Success bool
	Error   string
}

// RecordRun updates counters after a connector run.
func (r *Sources) RecordRun(ctx context.Context, id int64, o SourceRunOutcome) error {
	_, err := r.pool.Exec(ctx, `
		UPDATE articles_source SET
			total_runs = total_runs + 1,
			total_successes = total_successes + $2,
			total_failures = total_failures + $3,
			consecutive_failures = CASE WHEN $2 = 1 THEN 0 ELSE consecutive_failures + 1 END,
			last_checked_at = $4,
			last_success_at = CASE WHEN $2 = 1 THEN $4 ELSE last_success_at END,
			circuit_open_until = CASE
				WHEN $2 = 1 THEN NULL
				WHEN consecutive_failures + 1 >= 3 THEN $4 + INTERVAL '15 minutes'
				ELSE circuit_open_until
			END,
			last_error = $5
		WHERE id = $1`,
		id,
		boolToInt(o.Success),
		boolToInt(!o.Success),
		time.Now(),
		o.Error,
	)
	if err != nil {
		return fmt.Errorf("record source run: %w", err)
	}
	return nil
}

// EnsureExists inserts the source if absent (adopt-in-place: the production
// database already carries the source rows seeded by Django).
func (r *Sources) EnsureExists(ctx context.Context, key, name, baseURL string) (int64, error) {
	var id int64
	err := r.pool.QueryRow(ctx, `
		INSERT INTO articles_source (key, name, base_url)
		VALUES ($1, $2, $3)
		ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key
		RETURNING id`, key, name, baseURL).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("ensure source: %w", err)
	}
	return id, nil
}

// UpsertJournal inserts or finds a journal by name (used by connectors).
func (r *Sources) UpsertJournal(ctx context.Context, name string) (int64, error) {
	var id int64
	err := r.pool.QueryRow(ctx, `
		INSERT INTO articles_journal (name, issn, eissn, publisher)
		VALUES ($1, '', '', '')
		ON CONFLICT DO NOTHING
		RETURNING id`, name).Scan(&id)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			if err := r.pool.QueryRow(ctx,
				`SELECT id FROM articles_journal WHERE name = $1`, name).Scan(&id); err != nil {
				return 0, fmt.Errorf("find journal: %w", err)
			}
			return id, nil
		}
		return 0, fmt.Errorf("insert journal: %w", err)
	}
	return id, nil
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
