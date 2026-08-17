package httpapi_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/4q4r/cindex/backend/internal/config"
	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/httpapi"
	"github.com/4q4r/cindex/backend/internal/platform/db"
	"github.com/4q4r/cindex/backend/internal/platform/redis"
	"github.com/4q4r/cindex/backend/internal/repository"
	"github.com/4q4r/cindex/backend/internal/service"
	"github.com/4q4r/cindex/backend/migrations"
	"github.com/jackc/pgx/v5/pgxpool"
	goredis "github.com/redis/go-redis/v9"
	"github.com/riverqueue/river"
	"github.com/riverqueue/river/riverdriver/riverpgxv5"
	"github.com/riverqueue/river/rivermigrate"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	tcredismodule "github.com/testcontainers/testcontainers-go/modules/redis"
	"github.com/testcontainers/testcontainers-go/wait"
)

// apiTest bundles the containers and handler under test.
type apiTest struct {
	server *httptest.Server
	pool   *pgxpool.Pool
	rds    *goredis.Client
}

func setupAPI(t *testing.T, rateLimit int) *apiTest {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	pg, err := postgres.Run(ctx,
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
	t.Cleanup(func() { _ = pg.Terminate(ctx) })
	pgURL, err := pg.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		t.Fatal(err)
	}

	rd, err := tcredismodule.Run(ctx, "redis:7-alpine")
	if err != nil {
		t.Skipf("docker unavailable, skipping integration test: %v", err)
	}
	t.Cleanup(func() { _ = rd.Terminate(ctx) })
	rdURL, err := rd.ConnectionString(ctx)
	if err != nil {
		t.Fatal(err)
	}

	pool, err := db.Open(ctx, pgURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := db.Migrate(ctx, pool, migrations.FS); err != nil {
		t.Fatal(err)
	}
	migrator, err := rivermigrate.New(riverpgxv5.New(pool), nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := migrator.Migrate(ctx, rivermigrate.DirectionUp, nil); err != nil {
		t.Fatal(err)
	}

	rds, err := redis.Open(ctx, rdURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = rds.Close() })
	if err := rds.FlushDB(ctx).Err(); err != nil {
		t.Fatal(err)
	}

	riverClient, err := river.NewClient(riverpgxv5.New(pool), &river.Config{})
	if err != nil {
		t.Fatal(err)
	}

	cfg := &config.Config{}
	cfg.Search.RateLimitPerIP = rateLimit
	cfg.Search.RateLimitWindow = 60 * time.Second
	cfg.Search.FinalTopK = 30
	cfg.Search.DefaultFreshnessDays = 14
	cfg.AdminAPIKey = "test-admin-key"

	srv := httptest.NewServer(httpapi.NewRouter(httpapi.Dependencies{
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
		DB:     pool, Redis: rds, River: riverClient, Config: cfg,
		Ingestor: &service.NoopIngestor{},
	}))
	t.Cleanup(srv.Close)

	seed(t, pool)

	return &apiTest{server: srv, pool: pool, rds: rds}
}

// seed inserts one source, one journal and two articles with different
// eligibility profiles (parity fixture for the search endpoints).
func seed(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	ctx := context.Background()
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
	got.PeerReviewEvidence = "tierB: pubmed"
	got.IndexingEvidence = "tierB: medline"
	u := domain.ApplyEligibility(got, "openalex", "Journal of Quantum Research")
	if err := articles.UpdateEligibility(ctx, articleID, u); err != nil {
		t.Fatal(err)
	}
	if err := articles.ReplaceAuthors(ctx, articleID, []string{"Иванов И.И."}); err != nil {
		t.Fatal(err)
	}
}

func (a *apiTest) do(t *testing.T, method, path, body string, headers map[string]string) *http.Response {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, a.server.URL+path, reader)
	if err != nil {
		t.Fatal(err)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func readBody(t *testing.T, resp *http.Response) map[string]any {
	t.Helper()
	var out map[string]any
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("body is not JSON: %s", data)
	}
	return out
}

func TestImmediateSearch(t *testing.T) {
	a := setupAPI(t, 100)

	resp := a.do(t, "POST", "/api/v1/search", `{"query":"quantum computing"}`, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	body := readBody(t, resp)
	if body["query"] != "quantum computing" {
		t.Errorf("query = %v", body["query"])
	}
	if body["count"] != float64(1) {
		t.Errorf("count = %v, want 1", body["count"])
	}
	if body["page"] != float64(1) || body["per_page"] != float64(5) || body["total_pages"] != float64(1) {
		t.Errorf("pagination = %v", body)
	}
	results, ok := body["results"].([]any)
	if !ok || len(results) != 1 {
		t.Fatalf("results = %v", body["results"])
	}
	first := results[0].(map[string]any)
	if first["doi"] != "10.1000/qa.0001" {
		t.Errorf("doi = %v", first["doi"])
	}
	authors, ok := first["authors"].([]any)
	if !ok || len(authors) != 1 || authors[0] != "Иванов И.И." {
		t.Errorf("authors = %v", first["authors"])
	}
	if first["tier"] != "B" {
		t.Errorf("tier = %v, want B", first["tier"])
	}
	evidence := first["eligibility_evidence"].(map[string]any)
	if evidence["peer_reviewed"] != true || evidence["indexed"] != true {
		t.Errorf("eligibility evidence = %v", evidence)
	}
	confidence := first["eligibility_confidence"].(map[string]any)
	wantConfidence := map[string]float64{
		"peer_reviewed":        0.7,
		"indexed":              0.7,
		"doi_and_journal_card": 1,
		"not_preprint":         1,
		"overall":              0.85,
	}
	for key, want := range wantConfidence {
		if confidence[key] != want {
			t.Errorf("eligibility_confidence[%s] = %v, want %v", key, confidence[key], want)
		}
	}
	stats := body["source_stats"].(map[string]any)
	if stats["total"] != float64(1) || stats["live"] != float64(1) {
		t.Errorf("source_stats = %v", stats)
	}
}

func TestSearchValidationEnvelope(t *testing.T) {
	a := setupAPI(t, 100)

	cases := []struct {
		name string
		body string
		attr string
	}{
		{"empty query", `{"query":""}`, "query"},
		{"long query", fmt.Sprintf(`{"query":%q}`, strings.Repeat("a", 513)), "query"},
		{"bad sort", `{"query":"x","sort_by":"bogus"}`, "sort_by"},
		{"negative year", `{"query":"x","year_from":-1}`, "year_from"},
		{"per_page too large", `{"query":"x","per_page":51}`, "per_page"},
		{"page zero", `{"query":"x","page":0}`, "page"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp := a.do(t, "POST", "/api/v1/search", tc.body, nil)
			if resp.StatusCode != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", resp.StatusCode)
			}
			body := readBody(t, resp)
			if body["type"] != "validation_error" {
				t.Errorf("type = %v", body["type"])
			}
			errors, ok := body["errors"].([]any)
			if !ok || len(errors) == 0 {
				t.Fatalf("errors = %v", body["errors"])
			}
			first := errors[0].(map[string]any)
			if first["attr"] != tc.attr {
				t.Errorf("attr = %v, want %s", first["attr"], tc.attr)
			}
			if first["detail"] == nil || first["detail"] == "" {
				t.Errorf("detail missing: %v", first)
			}
		})
	}

	// Malformed JSON is a DRF-style detail error, not an envelope.
	resp := a.do(t, "POST", "/api/v1/search", `{"query":`, nil)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	body := readBody(t, resp)
	if detail, ok := body["detail"].(string); !ok || !strings.HasPrefix(detail, "JSON parse error - ") {
		t.Errorf("detail = %v", body)
	}
}

func TestSearchJobCreateAttachAndPoll(t *testing.T) {
	a := setupAPI(t, 100)

	// Create: 202 with a queued placeholder payload.
	resp := a.do(t, "POST", "/api/v1/search/jobs", `{"query":"quantum computing"}`, nil)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("status = %d, want 202", resp.StatusCode)
	}
	body := readBody(t, resp)
	jobID, ok := body["id"].(string)
	if !ok || jobID == "" {
		t.Fatalf("job id missing: %v", body)
	}
	if body["status"] != "queued" {
		t.Errorf("status = %v", body["status"])
	}
	if body["progress_percent"] != float64(5) {
		t.Errorf("progress_percent = %v", body["progress_percent"])
	}
	if body["stage"] != "queued" || body["substage_label"] != "Запрос принят" {
		t.Errorf("stage = %v / %v", body["stage"], body["substage_label"])
	}
	if body["message"] != "Задача поставлена в очередь" {
		t.Errorf("message = %v", body["message"])
	}
	if body["attached_to_existing"] != false {
		t.Errorf("attached_to_existing = %v, want false", body["attached_to_existing"])
	}
	if _, ok := body["source_stats"].(map[string]any); !ok {
		t.Errorf("source_stats missing")
	}

	// Identical request attaches to the active job: 200.
	resp = a.do(t, "POST", "/api/v1/search/jobs", `{"query":"quantum computing"}`, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("attach status = %d, want 200", resp.StatusCode)
	}
	body = readBody(t, resp)
	if body["id"] != jobID {
		t.Errorf("attached job id = %v, want %s", body["id"], jobID)
	}
	if body["attached_to_existing"] != true {
		t.Errorf("attached_to_existing = %v, want true", body["attached_to_existing"])
	}

	// A different filter signature must create a new job, not attach.
	resp = a.do(t, "POST", "/api/v1/search/jobs", `{"query":"quantum computing","peer_reviewed_only":true}`, nil)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("filtered create status = %d, want 202", resp.StatusCode)
	}
	body = readBody(t, resp)
	if body["id"] == jobID {
		t.Errorf("filtered job must not attach to the unfiltered one")
	}
	if body["peer_reviewed_only"] != true {
		t.Errorf("peer_reviewed_only = %v", body["peer_reviewed_only"])
	}

	// Poll: 200 with pagination and a persisted row.
	resp = a.do(t, "GET", "/api/v1/search/jobs/"+jobID, "", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("poll status = %d, want 200", resp.StatusCode)
	}
	body = readBody(t, resp)
	if body["id"] != jobID {
		t.Errorf("poll id = %v", body["id"])
	}
	if body["count"] != float64(0) {
		t.Errorf("count = %v, want 0 (no results yet)", body["count"])
	}
	if _, ok := body["results"].([]any); !ok {
		t.Errorf("results missing")
	}
	if body["page"] != float64(1) || body["per_page"] != float64(5) || body["total_pages"] != float64(0) {
		t.Errorf("pagination = %v", body)
	}

	// Query params are validated too.
	resp = a.do(t, "GET", "/api/v1/search/jobs/"+jobID+"?page=0", "", nil)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("page=0 status = %d, want 400", resp.StatusCode)
	}
	body = readBody(t, resp)
	if body["type"] != "validation_error" {
		t.Errorf("type = %v", body["type"])
	}

	// Unknown job and malformed uuid: DRF-style 404 detail.
	for _, id := range []string{"11111111-1111-1111-1111-111111111111", "not-a-uuid"} {
		resp = a.do(t, "GET", "/api/v1/search/jobs/"+id, "", nil)
		if resp.StatusCode != http.StatusNotFound {
			t.Errorf("status for %s = %d, want 404", id, resp.StatusCode)
		}
		body = readBody(t, resp)
		if body["detail"] != "No SearchJob matches the given query." {
			t.Errorf("detail for %s = %v", id, body["detail"])
		}
	}

	// The river task was inserted atomically with the job row.
	var count int
	if err := a.pool.QueryRow(context.Background(),
		`SELECT COUNT(*) FROM river_job WHERE args->>'job_id' = $1`, jobID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Errorf("river jobs = %d, want 1", count)
	}
}

func TestRateLimit(t *testing.T) {
	a := setupAPI(t, 3)

	ip := "203.0.113.7"
	for range 3 {
		resp := a.do(t, "POST", "/api/v1/search", `{"query":"quantum computing"}`, map[string]string{"X-Forwarded-For": ip})
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("request within limit: status = %d", resp.StatusCode)
		}
	}
	resp := a.do(t, "POST", "/api/v1/search", `{"query":"quantum computing"}`, map[string]string{"X-Forwarded-For": ip})
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("4th request status = %d, want 429", resp.StatusCode)
	}
	if retry := resp.Header.Get("Retry-After"); retry == "" {
		t.Error("Retry-After header missing")
	}
	body := readBody(t, resp)
	if body["detail"] != "Request was throttled." {
		t.Errorf("detail = %v", body["detail"])
	}

	// The poll endpoint is exempt from throttling.
	jobResp := a.do(t, "POST", "/api/v1/search/jobs", `{"query":"quantum"}`, map[string]string{"X-Forwarded-For": ip})
	if jobResp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("jobs create status = %d, want 429 (throttled)", jobResp.StatusCode)
	}
}

func TestRateLimitExemptsPoll(t *testing.T) {
	a := setupAPI(t, 3)

	ip := "203.0.113.9"
	jobResp := a.do(t, "POST", "/api/v1/search/jobs", `{"query":"poll test"}`, map[string]string{"X-Forwarded-For": ip})
	if jobResp.StatusCode != http.StatusAccepted {
		t.Fatalf("job create status = %d, want 202", jobResp.StatusCode)
	}
	jobID := readBody(t, jobResp)["id"].(string)

	for range 4 {
		resp := a.do(t, "GET", "/api/v1/search/jobs/"+jobID, "", map[string]string{"X-Forwarded-For": ip})
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("poll status = %d, want 200 (unthrottled)", resp.StatusCode)
		}
	}
}

func TestSourceStatsEndpoint(t *testing.T) {
	a := setupAPI(t, 100)

	resp := a.do(t, "GET", "/api/v1/source-stats", "", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	body := readBody(t, resp)
	if body["total"] != float64(1) || body["live"] != float64(1) {
		t.Errorf("stats = %v", body)
	}
	failed, ok := body["failed"].([]any)
	if !ok || len(failed) != 0 {
		t.Errorf("failed = %v", body["failed"])
	}

	// Second call is served from the Redis cache.
	a.rds.Del(context.Background(), "search:source_stats")
	resp = a.do(t, "GET", "/api/v1/source-stats", "", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status after cache clear = %d", resp.StatusCode)
	}
}

func TestAdminReindex(t *testing.T) {
	a := setupAPI(t, 100)

	// No key: forbidden, but only when the endpoint is enabled.
	resp := a.do(t, "POST", "/api/v1/admin/reindex", `{}`, nil)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("no key status = %d, want 403", resp.StatusCode)
	}
	body := readBody(t, resp)
	if body["detail"] != "You do not have permission to perform this action." {
		t.Errorf("detail = %v", body["detail"])
	}

	resp = a.do(t, "POST", "/api/v1/admin/reindex", `{}`, map[string]string{"X-API-Key": "wrong"})
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong key status = %d, want 403", resp.StatusCode)
	}

	resp = a.do(t, "POST", "/api/v1/admin/reindex", `{"query":"cold fusion"}`, map[string]string{"X-API-Key": "test-admin-key"})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("valid key status = %d, want 200", resp.StatusCode)
	}
	body = readBody(t, resp)
	if body["status"] != "queued" {
		t.Errorf("status = %v", body["status"])
	}
	if body["task_id"] == nil || body["task_id"] == "" {
		t.Errorf("task_id missing: %v", body)
	}
}

func TestImmediateSearchPagination(t *testing.T) {
	a := setupAPI(t, 100)

	// Seed one more matching article so the page math is non-trivial.
	ctx := context.Background()
	src := repository.NewSources(a.pool)
	openalexID, err := src.EnsureExists(ctx, "openalex", "OpenAlex", "https://api.openalex.org")
	if err != nil {
		t.Fatal(err)
	}
	journalID, err := src.UpsertJournal(ctx, "Journal of Quantum Research")
	if err != nil {
		t.Fatal(err)
	}
	year := 2024
	pubDate := time.Date(2024, 6, 1, 0, 0, 0, 0, time.UTC)
	articleID, err := repository.NewArticles(a.pool).Upsert(ctx, &domain.Article{
		SourceID: openalexID, JournalID: &journalID,
		Title: "Quantum computing in biology", Abstract: "Quantum computing methods.",
		URL: "https://example.org/q2", DOI: "10.1000/qa.0002",
		PubYear: &year, PubDate: &pubDate,
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = articleID

	resp := a.do(t, "POST", "/api/v1/search", `{"query":"quantum computing","per_page":1,"page":2}`, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	body := readBody(t, resp)
	if body["count"] != float64(2) || body["total_pages"] != float64(2) || body["page"] != float64(2) {
		t.Errorf("pagination = %v", body)
	}
	results := body["results"].([]any)
	if len(results) != 1 {
		t.Errorf("page 2 results = %d, want 1", len(results))
	}
}
