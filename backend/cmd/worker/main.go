// Command worker runs background jobs (River worker).
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/4q4r/cindex/backend/internal/config"
	"github.com/4q4r/cindex/backend/internal/httpapi"
	"github.com/4q4r/cindex/backend/internal/jobs"
	"github.com/4q4r/cindex/backend/internal/platform/db"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/riverqueue/river"
	"github.com/riverqueue/river/riverdriver/riverpgxv5"
	"github.com/riverqueue/river/rivermigrate"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(logger); err != nil {
		logger.Error("worker exited with error", "error", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	logger.Info("configuration loaded", "config", cfg.Redacted())

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	pool, err := db.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer pool.Close()

	migrator, err := rivermigrate.New(riverpgxv5.New(pool), nil)
	if err != nil {
		return err
	}
	if _, err := migrator.Migrate(ctx, rivermigrate.DirectionUp, &rivermigrate.MigrateOpts{}); err != nil {
		return err
	}
	logger.Info("river migrations applied")

	api := httpapi.NewAPI(cfg, logger, pool, nil, nil)
	workers := river.NewWorkers()
	river.AddWorker(workers, api.SearchJobWorker())

	client, err := river.NewClient(riverpgxv5.New(pool), &river.Config{
		Queues: map[string]river.QueueConfig{
			river.QueueDefault: {MaxWorkers: 8},
		},
		Workers: workers,
	})
	if err != nil {
		return err
	}
	if err := resumeSearchJobs(ctx, pool, client, logger); err != nil {
		return err
	}
	if err := client.Start(ctx); err != nil {
		return err
	}
	logger.Info("worker started")

	<-ctx.Done()
	logger.Info("shutdown signal received")
	if err := client.Stop(context.Background()); err != nil {
		return err
	}
	logger.Info("worker stopped cleanly")
	return nil
}

func resumeSearchJobs(
	ctx context.Context,
	pool *pgxpool.Pool,
	client *river.Client[pgx.Tx],
	logger *slog.Logger,
) error {
	rows, err := pool.Query(ctx, `
		SELECT search_job.id::text
		FROM search_searchjob AS search_job
		WHERE search_job.status IN ('queued', 'running')
			AND search_job.finished_at IS NULL
			AND NOT EXISTS (
				SELECT 1
				FROM river_job
				WHERE kind = 'search_job'
					AND args->>'job_id' = search_job.id::text
					AND finalized_at IS NULL
			)`)
	if err != nil {
		return fmt.Errorf("query unfinished search jobs: %w", err)
	}
	defer rows.Close()

	resumed := 0
	for rows.Next() {
		var jobID string
		if err := rows.Scan(&jobID); err != nil {
			return fmt.Errorf("scan unfinished search job: %w", err)
		}
		if _, err := client.Insert(ctx, &jobs.SearchJobTask{JobID: jobID}, nil); err != nil {
			return fmt.Errorf("resume search job %s: %w", jobID, err)
		}
		resumed++
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("iterate unfinished search jobs: %w", err)
	}
	if resumed > 0 {
		logger.Info("unfinished search jobs resumed", "count", resumed)
	}
	return nil
}
