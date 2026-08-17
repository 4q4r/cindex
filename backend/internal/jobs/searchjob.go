// Package jobs contains River worker jobs, including the async search job.
package jobs

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
	"github.com/4q4r/cindex/backend/internal/service"
	"github.com/riverqueue/river"
)

// SearchJobTask executes an async search job (parity with
// apps.search.tasks.run_search_job).
type SearchJobTask struct {
	JobID string `json:"job_id"`
}

// Kind returns the River job kind.
func (SearchJobTask) Kind() string { return "search_job" }

// SearchJobWorker runs SearchJobTask jobs.
type SearchJobWorker struct {
	river.WorkerDefaults[SearchJobTask]
	Jobs      *repository.SearchJobs
	Search    *service.Search
	Ingestor  service.Ingestor
	Enricher  service.QuoteExtractor
	Logger    *slog.Logger
	Freshness int
}

// Work executes one search job end to end.
func (w *SearchJobWorker) Work(ctx context.Context, job *river.Job[SearchJobTask]) error {
	jobID := job.Args.JobID
	logger := w.Logger
	if logger == nil {
		logger = slog.Default()
	}
	started := time.Now()

	dbJob, err := w.Jobs.GetByID(ctx, jobID)
	if errors.Is(err, repository.ErrNotFound) {
		logger.Warn("search job row missing; skipping", "job_id", jobID)
		return nil
	}
	if err != nil {
		return fmt.Errorf("load search job %s: %w", jobID, err)
	}
	if isTerminal(dbJob.Status) || dbJob.FinishedAt != nil {
		logger.Info("search job already finished; skipping", "job_id", jobID)
		return nil
	}

	update := func(fields map[string]any) {
		applyJobUpdate(dbJob, fields)
		if err := w.Jobs.UpdateProgress(ctx, dbJob); err != nil {
			logger.Error("persist job progress", "job_id", jobID, "error", err)
		}
	}

	fail := func(message string, err error) error {
		dbJob.Status = domain.JobStatusFailed
		dbJob.Stage = "failed"
		sub := domain.StageSubstage["failed"]
		dbJob.Substage = sub[0]
		dbJob.SubstageLabel = sub[1]
		dbJob.Message = message
		if err != nil {
			dbJob.Error = truncateError(err.Error())
		}
		now := time.Now()
		dbJob.FinishedAt = &now
		if err := w.Jobs.Update(ctx, dbJob); err != nil {
			logger.Error("persist failed job", "job_id", jobID, "error", err)
		}
		return err
	}

	// Status: running, stage: checking_index.
	update(map[string]any{
		"status":  domain.JobStatusRunning,
		"stage":   "checking_index",
		"message": "Проверяем, есть ли уже подходящие статьи в корпусе",
	})

	hitsBefore, err := w.Search.IndexHitCount(ctx, dbJob.Query, dbJob.Expression)
	if err != nil {
		return fail("Ошибка проверки корпуса", err)
	}
	freshness := w.Freshness
	if freshness <= 0 {
		freshness = 14
	}
	if freshness < 1 {
		freshness = 1
	}

	completedSourceKeys := sortedKeys(dbJob.SourceTimings)
	finishedLiveScan := dbJob.SourceTotal > 0 &&
		dbJob.SourceDone >= dbJob.SourceTotal &&
		len(dbJob.SourceTimings) > 0

	rescanTriggered, rescanReason := determineRescan(
		dbJob, hitsBefore, freshness, w.Jobs, ctx,
	)

	update(map[string]any{
		"index_hits_before":   hitsBefore,
		"freshness_days_used": freshness,
		"rescan_triggered":    rescanTriggered,
		"rescan_reason":       rescanReason,
	})

	if rescanTriggered {
		progress := buildProgressCallback(ctx, w.Jobs, dbJob, logger)
		profile := buildProfileCallback(ctx, w.Jobs, dbJob, logger)
		_, _, err := w.Ingestor.IngestQuery(ctx, dbJob.Query, service.IngestOptions{
			InitialDone:         dbJob.SourceDone,
			InitialFailed:       dbJob.SourceFailed,
			ResumeCompletedKeys: completedSourceKeys,
			Progress:            progress,
			Profile:             profile,
		})
		if err != nil {
			return fail("Ошибка сбора статей", err)
		}
	} else if finishedLiveScan {
		update(map[string]any{
			"stage":   "searching_index",
			"message": "Ранжируем найденные статьи и собираем выдачу",
		})
	} else {
		if err := w.recordSourceHealth(ctx, dbJob); err != nil {
			logger.Error("record source health", "job_id", jobID, "error", err)
		}
	}

	if dbJob.Stage != "searching_index" {
		update(map[string]any{
			"stage":   "searching_index",
			"message": "Ранжируем найденные статьи и собираем выдачу",
		})
	}

	results, _, err := w.Search.Run(ctx, dbJob.Query, dbJob.Expression, dbJob.Filters)
	if err != nil {
		return fail("Ошибка ранжирования", err)
	}
	if err := w.Search.AttachAuthors(ctx, results); err != nil {
		return fail("Ошибка загрузки авторов", err)
	}
	hitsAfter, err := w.Search.IndexHitCount(ctx, dbJob.Query, dbJob.Expression)
	if err != nil {
		return fail("Ошибка подсчёта результатов", err)
	}

	// PERELMAN quote extraction is a visible substage; the stage-2 enricher is
	// a no-op that fills quotes from the ArticleQuotes cache. Stage 5 wires
	// the real extractor.
	update(map[string]any{
		"stage":          "searching_index",
		"substage":       "quote_extraction",
		"substage_label": "Извлекаем цитаты (PERELMAN)",
		"message":        "Извлекаем релевантные цитаты из статей",
	})
	if w.Enricher != nil {
		if err := w.Enricher.Enrich(ctx, results); err != nil {
			logger.Warn("quote enrichment failed", "job_id", jobID, "error", err)
		}
	}

	finalStatus := domain.JobStatusCompleted
	finalMessage := "Готово"
	if len(dbJob.SourceFailed) > 0 {
		finalStatus = domain.JobStatusPartial
		finalMessage = partialMessage(len(dbJob.SourceFailed))
	}

	now := time.Now()
	dbJob.Status = finalStatus
	dbJob.Stage = "completed"
	sub := domain.StageSubstage["completed"]
	dbJob.Substage = sub[0]
	dbJob.SubstageLabel = sub[1]
	dbJob.Message = finalMessage
	dbJob.IndexHitsAfter = hitsAfter
	dbJob.Results = results
	dbJob.FinishedAt = &now
	if err := w.Jobs.Update(ctx, dbJob); err != nil {
		return fmt.Errorf("persist completed job: %w", err)
	}

	if err := w.Jobs.RecordWaitCompletion(ctx, dbJob.RescanTriggered, now.Sub(started).Seconds()); err != nil {
		logger.Error("record wait stat", "job_id", jobID, "error", err)
	}
	logger.Info("search job completed",
		"job_id", jobID,
		"status", finalStatus,
		"hits_before", hitsBefore,
		"hits_after", hitsAfter,
		"results", len(results),
		"duration_seconds", now.Sub(started).Seconds(),
	)
	return nil
}

// recordSourceHealth persists aggregated health counters when no rescan runs
// (parity with _record_source_health).
func (w *SearchJobWorker) recordSourceHealth(ctx context.Context, dbJob *domain.SearchJob) error {
	health, err := w.SourceHealth(ctx)
	if err != nil {
		return err
	}
	if len(health) > 0 {
		total, failed, live := service.ComputeSourceStats(health, w.SourceNames())
		dbJob.SourceTotal = total
		dbJob.SourceDone = total
		dbJob.SourceLive = live
		dbJob.SourceFailed = failed
		return w.Jobs.UpdateProgress(ctx, dbJob)
	}
	return nil
}

// SourceHealth returns the key->status map; SourceNames returns display names.
func (w *SearchJobWorker) SourceHealth(ctx context.Context) (map[string]string, error) {
	if stats, ok := w.Ingestor.(interface {
		SourceHealth(ctx context.Context) (map[string]string, error)
	}); ok {
		return stats.SourceHealth(ctx)
	}
	return map[string]string{}, nil
}

// SourceNames returns a key->display-name map for failed source reporting.
func (w *SearchJobWorker) SourceNames() map[string]string {
	return map[string]string{}
}

func determineRescan(
	dbJob *domain.SearchJob,
	hitsBefore int,
	freshnessDays int,
	jobs *repository.SearchJobs,
	ctx context.Context,
) (bool, string) {
	completedSourceKeys := sortedKeys(dbJob.SourceTimings)
	finishedLiveScan := dbJob.SourceTotal > 0 &&
		dbJob.SourceDone >= dbJob.SourceTotal &&
		len(dbJob.SourceTimings) > 0
	resumeLiveScan := len(completedSourceKeys) > 0 && !finishedLiveScan

	fresh, err := jobs.HasFreshRecentScan(ctx, dbJob.Query, freshnessDays, dbJob.ID)
	if err != nil {
		fresh = false
	}

	switch {
	case resumeLiveScan:
		return true, "resumed_after_restart"
	case dbJob.ForceRefreshRequested:
		return true, "forced_by_user"
	case hitsBefore <= 0:
		return true, "empty_index_hits"
	case !fresh:
		return true, "stale_query_scan"
	}
	return false, ""
}

func partialMessage(n int) string {
	var plural string
	switch {
	case n == 1:
		plural = "источник недоступен"
	case n < 5:
		plural = "источника недоступно"
	default:
		plural = "источников недоступно"
	}
	return fmt.Sprintf("Готово, но %d %s", n, plural)
}

func isTerminal(status string) bool {
	switch status {
	case domain.JobStatusCompleted, domain.JobStatusPartial, domain.JobStatusFailed:
		return true
	}
	return false
}

func truncateError(s string) string {
	if len(s) > 4000 {
		return s[:4000]
	}
	return s
}

func sortedKeys(m map[string]domain.SourceTiming) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0 && keys[j] < keys[j-1]; j-- {
			keys[j], keys[j-1] = keys[j-1], keys[j]
		}
	}
	return keys
}

func applyJobUpdate(j *domain.SearchJob, fields map[string]any) {
	if v, ok := fields["status"]; ok {
		if s, ok2 := v.(string); ok2 {
			j.Status = s
		}
	}
	if v, ok := fields["stage"]; ok {
		if s, ok2 := v.(string); ok2 {
			j.Stage = s
			if sub, ok3 := domain.StageSubstage[s]; ok3 {
				j.Substage = sub[0]
				j.SubstageLabel = sub[1]
			}
		}
	}
	if v, ok := fields["message"]; ok {
		if s, ok2 := v.(string); ok2 {
			j.Message = s
		}
	}
	if v, ok := fields["index_hits_before"]; ok {
		if n, ok2 := v.(int); ok2 {
			j.IndexHitsBefore = n
		}
	}
	if v, ok := fields["freshness_days_used"]; ok {
		if n, ok2 := v.(int); ok2 {
			j.FreshnessDaysUsed = n
		}
	}
	if v, ok := fields["rescan_triggered"]; ok {
		if b, ok2 := v.(bool); ok2 {
			j.RescanTriggered = b
		}
	}
	if v, ok := fields["rescan_reason"]; ok {
		if s, ok2 := v.(string); ok2 {
			j.RescanReason = s
		}
	}
	if v, ok := fields["substage"]; ok {
		if s, ok2 := v.(string); ok2 {
			j.Substage = s
		}
	}
	if v, ok := fields["substage_label"]; ok {
		if s, ok2 := v.(string); ok2 {
			j.SubstageLabel = s
		}
	}
}

func buildProgressCallback(ctx context.Context, jobs *repository.SearchJobs, dbJob *domain.SearchJob, logger *slog.Logger) func(service.ProgressEvent) {
	return func(event service.ProgressEvent) {
		live := dbJob.SourceLive
		if event.TotalSources > 0 {
			live = event.TotalSources - len(event.FailedSources)
			if live < 0 {
				live = 0
			}
		}
		dbJob.Stage = "live_scan"
		dbJob.Substage = event.Substage
		if event.SubstageLabel != "" {
			dbJob.SubstageLabel = event.SubstageLabel
		} else if s, ok := domain.StageSubstage["live_scan"]; ok {
			dbJob.SubstageLabel = s[1]
		}
		dbJob.Message = progressMessage(event)
		dbJob.SourceTotal = event.TotalSources
		dbJob.SourceDone = event.DoneSources
		dbJob.SourceLive = live
		dbJob.SourceFailed = event.FailedSources
		if err := jobs.UpdateProgress(ctx, dbJob); err != nil {
			logger.Error("persist live scan progress", "job_id", dbJob.ID, "error", err)
		}
	}
}

func progressMessage(event service.ProgressEvent) string {
	switch event.Substage {
	case "fetching", "enriching", "indexing":
		if event.TimingSource != "" {
			return fmt.Sprintf("%s · сейчас %s", event.SubstageLabel, event.TimingSource)
		}
	case "skipped":
		if event.TimingSource != "" {
			return "Источник пропущен: " + event.TimingSource
		}
		return "Источник пропущен"
	case "failed":
		if event.TimingSource != "" {
			return "Источник временно недоступен: " + event.TimingSource
		}
		return "Источник временно недоступен"
	case "completed":
		return fmt.Sprintf("Проверили %d из %d источников", event.DoneSources, event.TotalSources)
	}
	if event.TimingSource != "" {
		return "Проверяем корпус · сейчас " + event.TimingSource
	}
	return "Проверяем корпус"
}

func buildProfileCallback(ctx context.Context, jobs *repository.SearchJobs, dbJob *domain.SearchJob, logger *slog.Logger) func(service.ProfileEvent) {
	return func(event service.ProfileEvent) {
		if event.SourceKey == "" {
			return
		}
		if dbJob.SourceTimings == nil {
			dbJob.SourceTimings = map[string]domain.SourceTiming{}
		}
		timing := domain.SourceTiming{
			Status:        event.Status,
			ArticlesCount: event.ArticlesCount,
		}
		if !event.EndTime.IsZero() && !event.StartTime.IsZero() {
			timing.TotalSeconds = round2(event.EndTime.Sub(event.StartTime).Seconds())
		}
		dbJob.SourceTimings[event.SourceKey] = timing
		if err := jobs.UpdateProgress(ctx, dbJob); err != nil {
			logger.Error("persist source timing", "job_id", dbJob.ID, "error", err)
		}
	}
}

func round2(v float64) float64 {
	return math.Round(v*100) / 100
}
