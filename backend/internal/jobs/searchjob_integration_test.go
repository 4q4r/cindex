package jobs_test

import (
	"context"
	"log/slog"
	"testing"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/jobs"
	"github.com/4q4r/cindex/backend/internal/platform/db"
	"github.com/4q4r/cindex/backend/internal/repository"
	"github.com/4q4r/cindex/backend/internal/service"
	"github.com/4q4r/cindex/backend/migrations"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/riverqueue/river"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"
)

func setupPool(t *testing.T) *pgxpool.Pool {
	t.Helper()

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
	t.Cleanup(pool.Close)

	if err := db.Migrate(ctx, pool, migrations.FS); err != nil {
		t.Fatal(err)
	}
	return pool
}

// TestSearchJobWorkerEndToEnd runs the full job lifecycle: queued job ->
// corpus check -> no rescan (fresh corpus, hits present) -> searching_index ->
// completed with results persisted.
func TestSearchJobWorkerEndToEnd(t *testing.T) {
	ctx := context.Background()
	pool := setupPool(t)

	src := repository.NewSources(pool)
	openalexID, err := src.EnsureExists(ctx, "openalex", "OpenAlex", "https://api.openalex.org")
	if err != nil {
		t.Fatal(err)
	}
	journalID, err := src.UpsertJournal(ctx, "Journal of Quantum Research")
	if err != nil {
		t.Fatal(err)
	}
	articles := repository.NewArticles(pool)
	year := 2023
	pubDate := time.Date(2023, 3, 1, 0, 0, 0, 0, time.UTC)
	articleID, err := articles.Upsert(ctx, &domain.Article{
		SourceID: openalexID, JournalID: &journalID,
		Title: "Quantum computing advances", Abstract: "Quantum computing research.",
		URL: "https://example.org/q1", DOI: "10.1000/qa.0001",
		PubYear: &year, PubDate: &pubDate,
	})
	if err != nil {
		t.Fatal(err)
	}
	got, err := articles.GetByID(ctx, articleID)
	if err != nil {
		t.Fatal(err)
	}
	got.PeerReviewEvidence = "tierA: openalex venue=journal"
	got.IndexingEvidence = "tierA: medline"
	u := domain.ApplyEligibility(got, "openalex", "Journal of Quantum Research")
	if err := articles.UpdateEligibility(ctx, articleID, u); err != nil {
		t.Fatal(err)
	}
	if err := articles.ReplaceAuthors(ctx, articleID, []string{"Иванов И.И."}); err != nil {
		t.Fatal(err)
	}

	jobsRepo := repository.NewSearchJobs(pool)
	jobID := uuid.New().String()
	if err := jobsRepo.Create(ctx, &domain.SearchJob{
		ID: jobID, Query: "quantum computing", Expression: "",
		FreshnessDaysUsed: 14, Status: domain.JobStatusQueued,
		Stage: "queued", Substage: "queued", SubstageLabel: "Запрос принят",
		Message: "Задача поставлена в очередь", SourceFailed: []string{},
		SourceTimings: map[string]domain.SourceTiming{}, Results: []domain.SearchHit{},
		CreatedAt: time.Now(), UpdatedAt: time.Now(),
	}); err != nil {
		t.Fatal(err)
	}

	translate := service.NewTranslate()
	search := service.NewSearch(articles, repository.NewQuotes(pool), translate)
	search.SetTopK(30)
	worker := &jobs.SearchJobWorker{
		Jobs:      jobsRepo,
		Search:    search,
		Ingestor:  &service.NoopIngestor{Logger: slog.Default()},
		Logger:    slog.Default(),
		Freshness: 14,
	}

	riverJob := &river.Job[jobs.SearchJobTask]{Args: jobs.SearchJobTask{JobID: jobID}}
	if err := worker.Work(ctx, riverJob); err != nil {
		t.Fatal(err)
	}

	stored, err := jobsRepo.GetByID(ctx, jobID)
	if err != nil {
		t.Fatal(err)
	}
	if stored.Status != domain.JobStatusCompleted {
		t.Errorf("status = %q, want completed", stored.Status)
	}
	if stored.FinishedAt == nil {
		t.Error("finished_at must be set")
	}
	if stored.Stage != "completed" {
		t.Errorf("stage = %q", stored.Stage)
	}
	if stored.IndexHitsBefore != 1 || stored.IndexHitsAfter != 1 {
		t.Errorf("index hits before=%d after=%d, want 1/1", stored.IndexHitsBefore, stored.IndexHitsAfter)
	}
	if len(stored.Results) != 1 {
		t.Fatalf("results = %d, want 1", len(stored.Results))
	}
	if stored.Results[0].DOI != "10.1000/qa.0001" {
		t.Errorf("result doi = %q", stored.Results[0].DOI)
	}
	if len(stored.Results[0].Authors) != 1 || stored.Results[0].Authors[0] != "Иванов И.И." {
		t.Errorf("authors attached: %v", stored.Results[0].Authors)
	}
	if stored.Results[0].Tier == "" {
		t.Error("tier must be populated")
	}

	// Wait stats rolling average recorded once. The corpus has no fresh scan
	// for this query yet, so the job rescans and records the with-enrichment
	// bucket (parity with _determine_rescan stale_query_scan).
	stats, err := jobsRepo.WaitStats(ctx)
	if err != nil {
		t.Fatal(err)
	}
	s, ok := stats[domain.WaitStatWithEnrichment]
	if !ok || s.SampleCount != 1 {
		t.Errorf("wait stats: %+v", stats)
	}

	// Re-running on a finished job is a no-op.
	if err := worker.Work(ctx, riverJob); err != nil {
		t.Fatal(err)
	}
	stats, _ = jobsRepo.WaitStats(ctx)
	if s := stats[domain.WaitStatWithEnrichment]; s.SampleCount != 1 {
		t.Errorf("sample count after rerun = %d, want 1", s.SampleCount)
	}
}

// TestSearchJobWorkerFreshCorpusSkipsRescan verifies determineRescan: a job
// with hits in a fresh corpus does not trigger a live scan (rescan_triggered
// stays false, so the NoopIngestor is not consulted for a slow path).
func TestSearchJobWorkerFreshCorpusSkipsRescan(t *testing.T) {
	ctx := context.Background()
	pool := setupPool(t)

	src := repository.NewSources(pool)
	openalexID, err := src.EnsureExists(ctx, "openalex", "OpenAlex", "https://api.openalex.org")
	if err != nil {
		t.Fatal(err)
	}
	articles := repository.NewArticles(pool)
	year := 2023
	pubDate := time.Date(2023, 3, 1, 0, 0, 0, 0, time.UTC)
	articleID, err := articles.Upsert(ctx, &domain.Article{
		SourceID: openalexID, Title: "Quantum computing advances",
		URL: "https://example.org/q2", DOI: "10.1000/qa.0002",
		PubYear: &year, PubDate: &pubDate,
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = articleID

	jobsRepo := repository.NewSearchJobs(pool)
	jobID := uuid.New().String()
	if err := jobsRepo.Create(ctx, &domain.SearchJob{
		ID: jobID, Query: "quantum computing", Expression: "",
		FreshnessDaysUsed: 14, Status: domain.JobStatusQueued,
		Stage: "queued", Substage: "queued", SubstageLabel: "Запрос принят",
		Message: "Задача поставлена в очередь", SourceFailed: []string{},
		SourceTimings: map[string]domain.SourceTiming{}, Results: []domain.SearchHit{},
		CreatedAt: time.Now(), UpdatedAt: time.Now(),
	}); err != nil {
		t.Fatal(err)
	}

	// A recent completed job for the same query makes the corpus scan fresh,
	// so determineRescan must NOT trigger a live scan (parity with
	// _is_fresh_recent_scan + stale_query_scan). finished_at is persisted by
	// Update, not Create (as in the real completion flow).
	priorID := uuid.New().String()
	now := time.Now()
	prior := &domain.SearchJob{
		ID: priorID, Query: "quantum computing", Expression: "",
		FreshnessDaysUsed: 14, Status: domain.JobStatusCompleted,
		Stage: "completed", Substage: "done", SubstageLabel: "Готово",
		Message: "Готово", SourceFailed: []string{},
		SourceTimings: map[string]domain.SourceTiming{}, Results: []domain.SearchHit{},
		CreatedAt: now, UpdatedAt: now, FinishedAt: &now,
	}
	if err := jobsRepo.Create(ctx, prior); err != nil {
		t.Fatal(err)
	}
	if err := jobsRepo.Update(ctx, prior); err != nil {
		t.Fatal(err)
	}

	search := service.NewSearch(articles, repository.NewQuotes(pool), service.NewTranslate())
	search.SetTopK(30)
	worker := &jobs.SearchJobWorker{
		Jobs:      jobsRepo,
		Search:    search,
		Ingestor:  &service.NoopIngestor{Logger: slog.Default()},
		Enricher:  &service.CacheQuoteExtractor{Quotes: repository.NewQuotes(pool)},
		Logger:    slog.Default(),
		Freshness: 14,
	}

	if err := worker.Work(ctx, &river.Job[jobs.SearchJobTask]{Args: jobs.SearchJobTask{JobID: jobID}}); err != nil {
		t.Fatal(err)
	}

	stored, err := jobsRepo.GetByID(ctx, jobID)
	if err != nil {
		t.Fatal(err)
	}
	if stored.RescanTriggered {
		t.Errorf("rescan must not trigger for a fresh corpus with hits, reason=%q", stored.RescanReason)
	}
	if stored.Status != domain.JobStatusCompleted {
		t.Errorf("status = %q, want completed", stored.Status)
	}
}
