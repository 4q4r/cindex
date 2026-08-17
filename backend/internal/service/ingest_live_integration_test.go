package service

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/4q4r/cindex/backend/internal/connector"
	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/platform/db"
	"github.com/4q4r/cindex/backend/internal/repository"
	"github.com/4q4r/cindex/backend/migrations"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"
)

func setupIngestPool(t *testing.T) *pgxpool.Pool {
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
	t.Cleanup(func() { _ = container.Terminate(context.Background()) })

	databaseURL, err := container.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		t.Fatal(err)
	}
	pool, err := db.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := db.Migrate(ctx, pool, migrations.FS); err != nil {
		t.Fatal(err)
	}
	return pool
}

func TestLiveIngestorAJOLPipeline(t *testing.T) {
	pool := setupIngestPool(t)
	browser := newAJOLBrowserStub(t)
	defer browser.Close()

	articles := repository.NewArticles(pool)
	sources := repository.NewSources(pool)
	ingestor := &LiveIngestor{
		Registry: connector.NewRegistry(connector.Options{BrowserURL: browser.URL}),
		Sources:  sources, Articles: articles,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	var substages []string
	completed, failed, err := ingestor.IngestQuery(context.Background(), "public health", IngestOptions{
		SourceKeys:     []string{"ajol"},
		PerSourceLimit: 2,
		Progress: func(event ProgressEvent) {
			substages = append(substages, event.Substage)
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(failed) != 0 {
		t.Fatalf("failed sources = %v", failed)
	}
	if len(completed) != 1 || completed[0] != "ajol" {
		t.Fatalf("completed keys = %v, want [ajol]", completed)
	}
	for _, want := range []string{"queued", "fetching", "enriching", "indexing", "completed"} {
		if !containsString(substages, want) {
			t.Errorf("progress substages %v do not contain %q", substages, want)
		}
	}

	article, err := articles.GetByDOI(context.Background(), "10.5555/ajol.1")
	if err != nil {
		t.Fatal(err)
	}
	if article.Title != "A sufficiently long open access article title" {
		t.Errorf("article title = %q", article.Title)
	}
	if article.Abstract == "" || article.PubYear == nil || *article.PubYear != 2024 {
		t.Errorf("article enrichment missing: abstract=%q year=%v", article.Abstract, article.PubYear)
	}
	authors, err := articles.GetAuthors(context.Background(), article.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(authors) != 1 || authors[0] != "Unknown author" {
		t.Errorf("authors = %v, want Unknown author fallback", authors)
	}
	identifiers, err := articles.GetIdentifiers(context.Background(), []int64{article.ID})
	if err != nil {
		t.Fatal(err)
	}
	if len(identifiers[article.ID]) != 1 || identifiers[article.ID][0].Value != article.DOI {
		t.Errorf("identifiers = %v", identifiers[article.ID])
	}

	source, err := sources.GetByKey(context.Background(), "ajol")
	if err != nil {
		t.Fatal(err)
	}
	if source.TotalRuns != 1 || source.TotalSuccesses != 1 || source.ConsecutiveFailures != 0 {
		t.Errorf("source counters = %+v", source)
	}
	health, err := ingestor.SourceHealth(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if health["ajol"] != "healthy" || health["crossref"] != "never_queried" {
		t.Errorf("source health = %v", health)
	}

	if err := articles.ReplaceAuthors(context.Background(), article.ID, []string{"Existing Author"}); err != nil {
		t.Fatal(err)
	}
	_, candidateAuthors, err := ingestor.saveArticle(context.Background(), connector.RawArticle{
		SourceKey: "ajol", DOI: article.DOI, URL: article.URL, Title: article.Title,
		Journal: "African Test Journal", Year: article.PubYear,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(candidateAuthors) != 1 || candidateAuthors[0] != "Existing Author" {
		t.Errorf("candidate authors = %v, want persisted authors", candidateAuthors)
	}
	authors, err = articles.GetAuthors(context.Background(), article.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(authors) != 1 || authors[0] != "Existing Author" {
		t.Errorf("refresh without authors replaced persisted authors: %v", authors)
	}
}

func TestLiveIngestorSkipsOpenCircuit(t *testing.T) {
	pool := setupIngestPool(t)
	sources := repository.NewSources(pool)
	id, err := sources.EnsureExists(context.Background(), "ajol", "AJOL", "https://ajol.org")
	if err != nil {
		t.Fatal(err)
	}
	until := time.Now().Add(time.Hour)
	if _, err := pool.Exec(context.Background(),
		`UPDATE articles_source SET circuit_open_until = $2 WHERE id = $1`, id, until); err != nil {
		t.Fatal(err)
	}

	ingestor := &LiveIngestor{
		Registry: connector.NewRegistry(connector.Options{}),
		Sources:  sources, Articles: repository.NewArticles(pool),
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	var profileStatus string
	completed, failed, err := ingestor.IngestQuery(context.Background(), "query", IngestOptions{
		SourceKeys: []string{"ajol"},
		Profile: func(event ProfileEvent) {
			profileStatus = event.Status
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(completed) != 1 || completed[0] != "ajol" {
		t.Errorf("completed = %v", completed)
	}
	if len(failed) != 1 || failed[0] != "AJOL" {
		t.Errorf("failed = %v", failed)
	}
	if profileStatus != "skipped" {
		t.Errorf("profile status = %q, want skipped", profileStatus)
	}
}

func TestDoiEnrichmentPersistsCascade(t *testing.T) {
	pool := setupIngestPool(t)
	ctx := context.Background()
	sources := repository.NewSources(pool)
	articles := repository.NewArticles(pool)
	sourceID, err := sources.EnsureExists(ctx, "ajol", "AJOL", "https://ajol.org")
	if err != nil {
		t.Fatal(err)
	}
	journalID, err := sources.UpsertJournal(ctx, "African Test Journal")
	if err != nil {
		t.Fatal(err)
	}
	article := &domain.Article{
		SourceID: sourceID, JournalID: &journalID, DOI: "10.5555/enrich.1",
		Title: "Article needing DOI enrichment", URL: "https://example.test/article",
	}
	article.ID, err = articles.Upsert(ctx, article)
	if err != nil {
		t.Fatal(err)
	}
	if err := articles.ReplaceAuthors(ctx, article.ID, []string{"Unknown author"}); err != nil {
		t.Fatal(err)
	}

	service := &DoiEnrichmentService{
		Articles: articles,
		client: &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
			var body string
			switch req.URL.Host {
			case "api.crossref.org":
				body = `{"message":{"author":[{"given":"Ada","family":"Lovelace"}],"published-print":{"date-parts":[[2022]]},"volume":"7","page":"11-19"}}`
			case "api.openalex.org":
				body = `{"abstract_inverted_index":{"Useful":[0],"abstract":[1]},"biblio":{"issue":"3"}}`
			case "api.semanticscholar.org":
				body = `{"authors":[{"name":"Ignored Author"}],"abstract":"ignored","year":2020}`
			default:
				t.Fatalf("unexpected DOI enrichment host: %s", req.URL.Host)
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     make(http.Header),
				Body:       io.NopCloser(strings.NewReader(body)),
				Request:    req,
			}, nil
		})},
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	if updated := service.Enrich(ctx, []EnrichCandidate{{
		Article: article, Authors: []string{"Unknown author"},
	}}); updated != 1 {
		t.Fatalf("Enrich updated %d articles, want 1", updated)
	}

	got, err := articles.GetByDOI(ctx, article.DOI)
	if err != nil {
		t.Fatal(err)
	}
	if got.PubYear == nil || *got.PubYear != 2022 || got.Abstract != "Useful abstract" ||
		got.Volume != "7" || got.Issue != "3" || got.Pages != "11-19" {
		t.Errorf("enriched article = %+v", got)
	}
	authors, err := articles.GetAuthors(ctx, article.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(authors) != 1 || authors[0] != "Ada Lovelace" {
		t.Errorf("enriched authors = %v", authors)
	}
}

func TestPerelmanQuoteExtractorCachesAndFreezes(t *testing.T) {
	pool := setupIngestPool(t)
	ctx := context.Background()
	sources := repository.NewSources(pool)
	articles := repository.NewArticles(pool)
	quotes := repository.NewQuotes(pool)
	sourceID, err := sources.EnsureExists(ctx, "openalex", "OpenAlex", "https://openalex.org")
	if err != nil {
		t.Fatal(err)
	}
	journalID, err := sources.UpsertJournal(ctx, "Test Journal")
	if err != nil {
		t.Fatal(err)
	}
	year := 2025
	article := &domain.Article{
		SourceID: sourceID, JournalID: &journalID, DOI: "10.5555/perelman.1",
		Title: "A published article for extraction", Abstract: "The abstract.",
		FullText: "The central result is supported by evidence.", PubYear: &year,
		URL: "https://example.test/perelman", IsNotPreprintOrManuscript: true,
	}
	article.ID, err = articles.Upsert(ctx, article)
	if err != nil {
		t.Fatal(err)
	}
	if err := articles.ReplaceAuthors(ctx, article.ID, []string{"Test Author"}); err != nil {
		t.Fatal(err)
	}

	requests := 0
	llm := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests++
		_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"{\"tldr\":\"Краткий вывод\",\"quotes\":[{\"text\":\"The central result is supported by evidence.\",\"location\":\"full text\",\"relevance\":0.95,\"rationale\":\"Main result\"}],\"formulas\":[],\"figures\":[]}"}}]}`)
	}))
	defer llm.Close()
	client, err := NewLLMClient(LLMConfig{BaseURL: llm.URL, APIKey: "test", Model: "glm-test", Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	extractor := &PerelmanQuoteExtractor{
		Articles: articles, Quotes: quotes, Sources: sources,
		Perelman: NewPerelman(client, PerelmanConfig{}), LocalStore: NewLocalStore(dir),
		Model: "glm-test", Concurrency: 2,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	hits := []domain.SearchHit{{
		ID: article.ID, Source: "OpenAlex", Journal: "Test Journal",
		Authors: []string{"Test Author"}, NotPreprint: true,
	}}
	if err := extractor.Enrich(ctx, hits); err != nil {
		t.Fatal(err)
	}
	if len(hits[0].Quotes) != 1 || hits[0].Quotes[0].Relevance != 0.95 || hits[0].TLDR != "Краткий вывод" {
		t.Fatalf("enriched hit = %+v", hits[0])
	}
	cached, err := quotes.GetByArticle(ctx, article.ID)
	if err != nil {
		t.Fatal(err)
	}
	if cached.Status != domain.QuotesStatusDone || len(cached.Quotes) != 1 {
		t.Fatalf("cached extraction = %+v", cached)
	}
	stored, err := articles.GetByID(ctx, article.ID)
	if err != nil {
		t.Fatal(err)
	}
	if stored.LocalMDPath == "" || !extractor.LocalStore.Exists(article.DOI) {
		t.Fatalf("article was not frozen: path=%q", stored.LocalMDPath)
	}

	second := []domain.SearchHit{{ID: article.ID}}
	if err := extractor.Enrich(ctx, second); err != nil {
		t.Fatal(err)
	}
	if requests != 1 || len(second[0].Quotes) != 1 {
		t.Fatalf("cache was not reused: requests=%d hit=%+v", requests, second[0])
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func newAJOLBrowserStub(t *testing.T) *httptest.Server {
	t.Helper()
	const oai = `<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/">
  <ListRecords>
    <record><header><identifier>oai:ajol:1</identifier></header><metadata><oai_dc:dc>
      <dc:title>A sufficiently long open access article title</dc:title>
      <dc:description>Open access research abstract about public health.</dc:description>
      <dc:identifier>https://www.ajol.info/index.php/test/article/view/1</dc:identifier>
      <dc:identifier>https://doi.org/10.5555/ajol.1</dc:identifier>
      <dc:source>African Test Journal</dc:source><dc:date>2024</dc:date>
      <dc:rights>Open Access</dc:rights>
    </oai_dc:dc></metadata></record>
  </ListRecords>
</OAI-PMH>`
	const landing = `<!doctype html><html><head>
<meta name="citation_title" content="A sufficiently long open access article title">
<meta name="citation_doi" content="10.5555/ajol.1">
<meta name="citation_journal_title" content="African Test Journal">
<meta name="citation_publication_date" content="2024">
<meta name="description" content="Open access research abstract about public health.">
</head><body><article><p>Open access full article body.</p></article></body></html>`

	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/fetch" {
			http.NotFound(w, r)
			return
		}
		var request struct {
			URL string `json:"url"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Errorf("decode browser request: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		body := landing
		contentType := "text/html"
		if strings.Contains(request.URL, "/oai") {
			body = oai
			contentType = "application/xml"
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status": 200, "body": body, "content_type": contentType,
		})
	}))
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
