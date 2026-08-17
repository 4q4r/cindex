package repository_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/platform/db"
	"github.com/4q4r/cindex/backend/internal/repository"
	"github.com/4q4r/cindex/backend/migrations"
	"github.com/jackc/pgx/v5/pgxpool"
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

func TestArticleLifecycle(t *testing.T) {
	ctx := context.Background()
	pool := setupPool(t)

	src := repository.NewSources(pool)
	srcID, err := src.EnsureExists(ctx, "openalex", "OpenAlex", "https://api.openalex.org")
	if err != nil {
		t.Fatal(err)
	}
	journalID, err := src.UpsertJournal(ctx, "Journal of Testing")
	if err != nil {
		t.Fatal(err)
	}

	articles := repository.NewArticles(pool)
	year := 2024
	pubDate := time.Date(2024, 5, 1, 0, 0, 0, 0, time.UTC)
	a := &domain.Article{
		SourceID: srcID, JournalID: &journalID,
		Title: "Testing Go repository", Abstract: "Abstract here",
		URL: "https://example.org/a1", DOI: "10.1234/go.0001",
		PubYear: &year, PubDate: &pubDate,
	}
	id, err := articles.Upsert(ctx, a)
	if err != nil {
		t.Fatal(err)
	}

	got, err := articles.GetByID(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if got.Title != a.Title || got.DOI != a.DOI {
		t.Errorf("roundtrip mismatch: %+v", got)
	}

	// Upsert keyed on DOI updates, does not duplicate.
	a.Title = "Updated title"
	id2, err := articles.Upsert(ctx, a)
	if err != nil {
		t.Fatal(err)
	}
	if id2 != id {
		t.Errorf("upsert created a new row: %d != %d", id2, id)
	}
	got, err = articles.GetByDOI(ctx, a.DOI)
	if err != nil {
		t.Fatal(err)
	}
	if got.Title != "Updated title" {
		t.Errorf("title = %q", got.Title)
	}

	if _, err := articles.GetByURL(ctx, a.URL); err != nil {
		t.Errorf("get by url: %v", err)
	}
	if _, err := articles.GetByDOI(ctx, "10.1234/nope"); !errors.Is(err, repository.ErrNotFound) {
		t.Errorf("missing doi err = %v, want ErrNotFound", err)
	}

	// Eligibility apply parity: tierA signals for peer-review and indexing
	// make the article fully eligible (all four confidences 1.0).
	got.PeerReviewEvidence = "tierA: openalex venue=journal"
	got.IndexingEvidence = "tierA: medline"
	u := domain.ApplyEligibility(got, "openalex", "Journal of Testing")
	if err := articles.UpdateEligibility(ctx, id, u); err != nil {
		t.Fatal(err)
	}
	got, _ = articles.GetByID(ctx, id)
	if !got.IsEligible {
		t.Errorf("expected eligible: %+v", got)
	}
	if got.EligibilityConfidence != 1.0 {
		t.Errorf("eligibility confidence = %v", got.EligibilityConfidence)
	}
	if got.PeerReviewEvidence != "tierA: openalex venue=journal" {
		t.Errorf("tier evidence must be preserved verbatim, got %q", got.PeerReviewEvidence)
	}

	// Authors.
	if err := articles.ReplaceAuthors(ctx, id, []string{"Иванов И.И.", "Петров П.П.", "Сидоров С.С."}); err != nil {
		t.Fatal(err)
	}
	names, err := articles.GetAuthors(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if len(names) != 3 || names[1] != "Петров П.П." {
		t.Errorf("authors = %v", names)
	}
	if err := articles.ReplaceAuthors(ctx, id, []string{"Новая Фамилия"}); err != nil {
		t.Fatal(err)
	}
	names, _ = articles.GetAuthors(ctx, id)
	if len(names) != 1 {
		t.Errorf("replace authors failed: %v", names)
	}

	// Identifiers (get_or_create parity).
	if err := articles.UpsertIdentifier(ctx, id, "hal", "hal-0000001"); err != nil {
		t.Fatal(err)
	}
	if err := articles.UpsertIdentifier(ctx, id, "hal", "hal-0000001"); err != nil {
		t.Fatal(err) // idempotent
	}
}

func TestSourceCircuitBreakerCounters(t *testing.T) {
	ctx := context.Background()
	pool := setupPool(t)

	src := repository.NewSources(pool)
	id, err := src.EnsureExists(ctx, "arxiv", "arXiv", "https://export.arxiv.org")
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := src.RecordRun(ctx, id, repository.SourceRunOutcome{Success: true}); err != nil {
			t.Fatal(err)
		}
	}
	if err := src.RecordRun(ctx, id, repository.SourceRunOutcome{Success: false, Error: "boom"}); err != nil {
		t.Fatal(err)
	}

	s, err := src.GetByKey(ctx, "arxiv")
	if err != nil {
		t.Fatal(err)
	}
	if s.TotalRuns != 3 || s.TotalSuccesses != 2 || s.TotalFailures != 1 {
		t.Errorf("counters: %+v", s)
	}
	if s.ConsecutiveFailures != 1 || s.LastError != "boom" {
		t.Errorf("failure state: %+v", s)
	}

	if err := src.RecordRun(ctx, id, repository.SourceRunOutcome{Success: true}); err != nil {
		t.Fatal(err)
	}
	s, _ = src.GetByKey(ctx, "arxiv")
	if s.ConsecutiveFailures != 0 {
		t.Errorf("consecutive failures must reset on success: %d", s.ConsecutiveFailures)
	}
}

func TestQuotesClaimAndPersist(t *testing.T) {
	ctx := context.Background()
	pool := setupPool(t)

	src := repository.NewSources(pool)
	srcID, err := src.EnsureExists(ctx, "zenodo", "Zenodo", "https://zenodo.org")
	if err != nil {
		t.Fatal(err)
	}
	articles := repository.NewArticles(pool)
	a := &domain.Article{
		SourceID: srcID, Title: "Quote me", DOI: "10.1234/go.0002",
		URL: "https://example.org/a2",
	}
	articleID, err := articles.Upsert(ctx, a)
	if err != nil {
		t.Fatal(err)
	}

	quotes := repository.NewQuotes(pool)

	// First claim wins.
	row, won, err := quotes.Claim(ctx, articleID)
	if err != nil {
		t.Fatal(err)
	}
	if !won || row.Status != domain.QuotesStatusPending {
		t.Fatalf("first claim: won=%v row=%+v", won, row)
	}

	// Second claim loses and does not overwrite.
	_, won, err = quotes.Claim(ctx, articleID)
	if err != nil {
		t.Fatal(err)
	}
	if won {
		t.Fatal("second claim must not win")
	}

	if err := quotes.SetDone(ctx, articleID, []domain.Quote{
		{Text: "verbatim quote", Location: "p.5", Relevance: "high", Rationale: "core claim"},
	}, "Краткое резюме.", "glm-test"); err != nil {
		t.Fatal(err)
	}

	cached, err := quotes.GetByArticle(ctx, articleID)
	if err != nil {
		t.Fatal(err)
	}
	if cached.Status != domain.QuotesStatusDone || cached.TLDR != "Краткое резюме." {
		t.Errorf("cached row: %+v", cached)
	}
	if len(cached.Quotes) != 1 || cached.Quotes[0].Text != "verbatim quote" {
		t.Errorf("quotes: %+v", cached.Quotes)
	}

	if err := quotes.SetFailed(ctx, articleID, "glm-test", "llm timeout"); err != nil {
		t.Fatal(err)
	}
	row, err = quotes.GetByArticle(ctx, articleID)
	if err != nil {
		t.Fatal(err)
	}
	if row.Status != domain.QuotesStatusFailed || row.Error != "llm timeout" {
		t.Errorf("failed row: %+v", row)
	}
}
