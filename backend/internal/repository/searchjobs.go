package repository

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// SearchJobs persists search jobs and rolling wait-time statistics.
type SearchJobs struct {
	pool *pgxpool.Pool
}

// NewSearchJobs builds the search-job repository over the given pool.
func NewSearchJobs(pool *pgxpool.Pool) *SearchJobs {
	return &SearchJobs{pool: pool}
}

const searchJobCols = `
	id, query, expression, force_refresh_requested, freshness_days_used, status,
	stage, substage, substage_label, message, source_total, source_done,
	source_live, source_failed, source_timings, index_hits_before,
	index_hits_after, rescan_triggered, rescan_reason, results, error,
	peer_reviewed_only, indexed_only, exclude_preprints, exclude_retracted,
	year_from, year_to, sort_by, created_at, updated_at, finished_at`

func scanSearchJob(row pgx.Row) (*domain.SearchJob, error) {
	var j domain.SearchJob
	var failedJSON []byte
	var timingsJSON []byte
	var resultsJSON []byte
	err := row.Scan(
		&j.ID, &j.Query, &j.Expression, &j.ForceRefreshRequested,
		&j.FreshnessDaysUsed, &j.Status, &j.Stage, &j.Substage, &j.SubstageLabel,
		&j.Message, &j.SourceTotal, &j.SourceDone, &j.SourceLive, &failedJSON,
		&timingsJSON, &j.IndexHitsBefore, &j.IndexHitsAfter, &j.RescanTriggered,
		&j.RescanReason, &resultsJSON, &j.Error,
		&j.Filters.PeerReviewedOnly, &j.Filters.IndexedOnly,
		&j.Filters.ExcludePreprints, &j.Filters.ExcludeRetracted,
		&j.Filters.YearFrom, &j.Filters.YearTo, &j.Filters.SortBy,
		&j.CreatedAt, &j.UpdatedAt, &j.FinishedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("scan search job: %w", err)
	}
	if len(failedJSON) > 0 {
		if err := json.Unmarshal(failedJSON, &j.SourceFailed); err != nil {
			return nil, fmt.Errorf("unmarshal source_failed: %w", err)
		}
	}
	if len(timingsJSON) > 0 {
		if err := json.Unmarshal(timingsJSON, &j.SourceTimings); err != nil {
			return nil, fmt.Errorf("unmarshal source_timings: %w", err)
		}
	}
	if len(resultsJSON) > 0 {
		if err := json.Unmarshal(resultsJSON, &j.Results); err != nil {
			return nil, fmt.Errorf("unmarshal results: %w", err)
		}
	}
	return &j, nil
}

func marshalOrEmpty[T any](v T) ([]byte, error) {
	payload, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	if payload == nil {
		payload = []byte("{}")
	}
	return payload, nil
}

type execer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// Create inserts a fresh queued search job.
func (r *SearchJobs) Create(ctx context.Context, j *domain.SearchJob) error {
	return r.create(ctx, r.pool, j)
}

// CreateTx inserts the job inside an existing transaction so it stays atomic
// with the River job row.
func (r *SearchJobs) CreateTx(ctx context.Context, tx pgx.Tx, j *domain.SearchJob) error {
	return r.create(ctx, tx, j)
}

func (r *SearchJobs) create(ctx context.Context, db execer, j *domain.SearchJob) error {
	failedJSON, err := marshalOrEmpty(j.SourceFailed)
	if err != nil {
		return fmt.Errorf("marshal source_failed: %w", err)
	}
	timingsJSON, err := marshalOrEmpty(j.SourceTimings)
	if err != nil {
		return fmt.Errorf("marshal source_timings: %w", err)
	}
	resultsJSON, err := marshalOrEmpty(j.Results)
	if err != nil {
		return fmt.Errorf("marshal results: %w", err)
	}
	now := time.Now()
	_, err = db.Exec(ctx, `
		INSERT INTO search_searchjob (
			id, query, expression, force_refresh_requested, freshness_days_used,
			status, stage, substage, substage_label, message, source_total,
			source_done, source_live, source_failed, source_timings,
			index_hits_before, index_hits_after, rescan_triggered, rescan_reason,
			results, error, peer_reviewed_only, indexed_only, exclude_preprints,
			exclude_retracted, year_from, year_to, sort_by, created_at, updated_at
		) VALUES (
			$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
			$16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28,
			$29, $30
		)`,
		j.ID, j.Query, j.Expression, j.ForceRefreshRequested,
		j.FreshnessDaysUsed, j.Status, j.Stage, j.Substage, j.SubstageLabel,
		j.Message, j.SourceTotal, j.SourceDone, j.SourceLive, failedJSON,
		timingsJSON, j.IndexHitsBefore, j.IndexHitsAfter, j.RescanTriggered,
		j.RescanReason, resultsJSON, j.Error, j.Filters.PeerReviewedOnly,
		j.Filters.IndexedOnly, j.Filters.ExcludePreprints,
		j.Filters.ExcludeRetracted, j.Filters.YearFrom, j.Filters.YearTo,
		j.Filters.SortBy, now, now,
	)
	if err != nil {
		return fmt.Errorf("create search job: %w", err)
	}
	return nil
}

// GetByID loads one search job.
func (r *SearchJobs) GetByID(ctx context.Context, id string) (*domain.SearchJob, error) {
	return scanSearchJob(r.pool.QueryRow(ctx,
		`SELECT `+searchJobCols+` FROM search_searchjob WHERE id = $1`, id))
}

// FindActive returns the newest active job (queued/running) matching the
// normalized query/expression, force-refresh flag and filter signature.
// Parity with apps.search.views._find_active_search_job.
func (r *SearchJobs) FindActive(ctx context.Context, query, expression string, forceRefresh bool, filters domain.SearchFilters) (*domain.SearchJob, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT `+searchJobCols+`
		FROM search_searchjob
		WHERE force_refresh_requested = $1 AND status IN ('queued', 'running')
		ORDER BY created_at DESC`, forceRefresh)
	if err != nil {
		return nil, fmt.Errorf("query active jobs: %w", err)
	}
	defer rows.Close()

	normQuery := normalizeJobText(query)
	normExpression := normalizeJobText(expression)
	target := filters.Signature()
	for rows.Next() {
		job, err := scanSearchJob(rows)
		if err != nil {
			return nil, fmt.Errorf("scan active job: %w", err)
		}
		if normalizeJobText(job.Query) == normQuery &&
			normalizeJobText(job.Expression) == normExpression &&
			job.Filters.Signature() == target {
			return job, nil
		}
	}
	return nil, rows.Err()
}

// Update writes the mutable job fields (full-row update used by the worker).
func (r *SearchJobs) Update(ctx context.Context, j *domain.SearchJob) error {
	failedJSON, err := marshalOrEmpty(j.SourceFailed)
	if err != nil {
		return fmt.Errorf("marshal source_failed: %w", err)
	}
	timingsJSON, err := marshalOrEmpty(j.SourceTimings)
	if err != nil {
		return fmt.Errorf("marshal source_timings: %w", err)
	}
	resultsJSON, err := marshalOrEmpty(j.Results)
	if err != nil {
		return fmt.Errorf("marshal results: %w", err)
	}
	_, err = r.pool.Exec(ctx, `
		UPDATE search_searchjob SET
			status = $2, stage = $3, substage = $4, substage_label = $5,
			message = $6, source_total = $7, source_done = $8, source_live = $9,
			source_failed = $10, source_timings = $11, index_hits_before = $12,
			index_hits_after = $13, rescan_triggered = $14, rescan_reason = $15,
			results = $16, error = $17, freshness_days_used = $18,
			updated_at = $19, finished_at = $20
		WHERE id = $1`,
		j.ID, j.Status, j.Stage, j.Substage, j.SubstageLabel, j.Message,
		j.SourceTotal, j.SourceDone, j.SourceLive, failedJSON, timingsJSON,
		j.IndexHitsBefore, j.IndexHitsAfter, j.RescanTriggered, j.RescanReason,
		resultsJSON, j.Error, j.FreshnessDaysUsed, time.Now(), j.FinishedAt,
	)
	if err != nil {
		return fmt.Errorf("update search job: %w", err)
	}
	return nil
}

// HasFreshRecentScan reports whether a recent completed/partial job for the
// query exists within the freshness window (parity with
// apps.search.tasks._is_fresh_recent_scan).
func (r *SearchJobs) HasFreshRecentScan(ctx context.Context, query string, freshnessDays int, excludeID string) (bool, error) {
	var finishedAt *time.Time
	err := r.pool.QueryRow(ctx, `
		SELECT finished_at FROM search_searchjob
		WHERE query = $1 AND status IN ('completed', 'partial') AND id <> $2
		ORDER BY finished_at DESC NULLS LAST LIMIT 1`, query, excludeID).Scan(&finishedAt)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && finishedAt == nil) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("query recent scan: %w", err)
	}
	return finishedAt.After(time.Now().AddDate(0, 0, -freshnessDays)), nil
}

// UpdateProgress persists a progress snapshot (stage/substage/message/счётчики).
func (r *SearchJobs) UpdateProgress(ctx context.Context, j *domain.SearchJob) error {
	failedJSON, err := marshalOrEmpty(j.SourceFailed)
	if err != nil {
		return fmt.Errorf("marshal source_failed: %w", err)
	}
	timingsJSON, err := marshalOrEmpty(j.SourceTimings)
	if err != nil {
		return fmt.Errorf("marshal source_timings: %w", err)
	}
	_, err = r.pool.Exec(ctx, `
		UPDATE search_searchjob SET
			stage = $2, substage = $3, substage_label = $4, message = $5,
			source_total = $6, source_done = $7, source_live = $8,
			source_failed = $9, source_timings = $10, index_hits_before = $11,
			index_hits_after = $12, rescan_triggered = $13, rescan_reason = $14,
			freshness_days_used = $15, updated_at = $16
		WHERE id = $1`,
		j.ID, j.Stage, j.Substage, j.SubstageLabel, j.Message, j.SourceTotal,
		j.SourceDone, j.SourceLive, failedJSON, timingsJSON, j.IndexHitsBefore,
		j.IndexHitsAfter, j.RescanTriggered, j.RescanReason,
		j.FreshnessDaysUsed, time.Now(),
	)
	if err != nil {
		return fmt.Errorf("update job progress: %w", err)
	}
	return nil
}

func normalizeJobText(v string) string {
	return strings.Join(strings.Fields(v), " ")
}

// WaitStats reads the two rolling average rows (parity with
// apps.search.progress.get_search_wait_stats).
func (r *SearchJobs) WaitStats(ctx context.Context) (map[string]domain.SearchWaitStat, error) {
	stats := make(map[string]domain.SearchWaitStat, 2)
	for _, kind := range []string{domain.WaitStatWithoutEnrichment, domain.WaitStatWithEnrichment} {
		var s domain.SearchWaitStat
		err := r.pool.QueryRow(ctx, `
			INSERT INTO search_searchwaitstat (kind, average_seconds, sample_count, created_at, updated_at)
			VALUES ($1, 0, 0, $2, $2)
			ON CONFLICT (kind) DO NOTHING`, kind, time.Now()).Scan()
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("ensure wait stat %s: %w", kind, err)
		}
		err = r.pool.QueryRow(ctx, `
			SELECT kind, average_seconds, sample_count
			FROM search_searchwaitstat WHERE kind = $1`, kind).
			Scan(&s.Kind, &s.AverageSeconds, &s.SampleCount)
		if err != nil {
			return nil, fmt.Errorf("load wait stat %s: %w", kind, err)
		}
		stats[kind] = s
	}
	return stats, nil
}

// RecordWaitCompletion updates the rolling average for one scan mode
// (parity with SearchWaitStat.record_completion: new_avg = (old + dur) / 2).
func (r *SearchJobs) RecordWaitCompletion(ctx context.Context, rescanTriggered bool, durationSeconds float64) error {
	if durationSeconds < 0 {
		return nil
	}
	kind := domain.WaitStatWithoutEnrichment
	if rescanTriggered {
		kind = domain.WaitStatWithEnrichment
	}
	_, err := r.pool.Exec(ctx, `
		INSERT INTO search_searchwaitstat (kind, average_seconds, sample_count, created_at, updated_at)
		VALUES ($1, $2, 1, $3, $3)
		ON CONFLICT (kind) DO UPDATE SET
			average_seconds = (search_searchwaitstat.average_seconds + EXCLUDED.average_seconds) / 2.0,
			sample_count = search_searchwaitstat.sample_count + 1,
			updated_at = $3`,
		kind, durationSeconds, time.Now())
	if err != nil {
		return fmt.Errorf("record wait completion: %w", err)
	}
	return nil
}
