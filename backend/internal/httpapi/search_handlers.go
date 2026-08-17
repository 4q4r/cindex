package httpapi

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/4q4r/cindex/backend/internal/config"
	"github.com/4q4r/cindex/backend/internal/connector"
	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/jobs"
	"github.com/4q4r/cindex/backend/internal/repository"
	"github.com/4q4r/cindex/backend/internal/service"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/oapi-codegen/runtime/types"
	"github.com/redis/go-redis/v9"
	"github.com/riverqueue/river"
)

const (
	sourceStatsCacheKey = "search:source_stats"
	sourceStatsTTL      = 15 * time.Second
	jobLockTTL          = 30 * time.Second
)

// API implements the generated ServerInterface.
type API struct {
	cfg    *config.Config
	logger *slog.Logger
	db     *pgxpool.Pool
	redis  *redis.Client

	articles *repository.Articles
	quotes   *repository.Quotes
	sources  *repository.Sources
	jobs     *repository.SearchJobs

	search    *service.Search
	translate *service.Translate
	stats     *service.SourceStatsService
	ingest    service.Ingestor
	enricher  service.QuoteExtractor
	river     *river.Client[pgx.Tx]
}

// NewAPI builds the search API over the generated handlers.
func NewAPI(
	cfg *config.Config,
	logger *slog.Logger,
	db *pgxpool.Pool,
	redisClient *redis.Client,
	riverClient *river.Client[pgx.Tx],
) *API {
	a := &API{
		cfg:       cfg,
		logger:    logger,
		db:        db,
		redis:     redisClient,
		articles:  repository.NewArticles(db),
		quotes:    repository.NewQuotes(db),
		sources:   repository.NewSources(db),
		jobs:      repository.NewSearchJobs(db),
		translate: service.NewTranslate(),
		river:     riverClient,
	}
	a.search = service.NewSearch(a.articles, a.quotes, a.translate)
	a.search.SetTopK(a.cfg.Search.FinalTopK)
	a.stats = &service.SourceStatsService{Sources: a.sources}
	a.enricher = &service.CacheQuoteExtractor{Quotes: a.quotes}
	configuredLLMFields := 0
	for _, value := range []string{cfg.LLM.BaseURL, cfg.LLM.APIKey, cfg.LLM.Model} {
		if value != "" {
			configuredLLMFields++
		}
	}
	if configuredLLMFields > 0 && configuredLLMFields < 3 {
		logger.Warn("PERELMAN configuration incomplete; using cache only")
	}
	if cfg.LLM.BaseURL != "" && cfg.LLM.APIKey != "" && cfg.LLM.Model != "" {
		client, err := service.NewLLMClient(service.LLMConfig{
			BaseURL: cfg.LLM.BaseURL, APIKey: cfg.LLM.APIKey, Model: cfg.LLM.Model,
			Timeout: cfg.LLM.Timeout, Temperature: cfg.LLM.Temperature,
			ExtraBody: cfg.LLM.ExtraBody, RequestInterval: cfg.LLM.RequestInterval,
		})
		if err != nil {
			logger.Error("PERELMAN configuration rejected; using cache only", "error", err)
		} else {
			a.enricher = &service.PerelmanQuoteExtractor{
				Articles: a.articles, Quotes: a.quotes, Sources: a.sources,
				Perelman: service.NewPerelman(client, service.PerelmanConfig{
					MaxQuotes: cfg.LLM.MaxQuotes, MaxInputChars: cfg.LLM.MaxInputChars,
				}),
				LocalStore: service.NewLocalStore(cfg.ArticlesDir), Model: cfg.LLM.Model,
				Concurrency: cfg.LLM.Concurrency, Logger: logger,
			}
		}
	}
	registry := connector.NewRegistry(connector.Options{
		BrowserURL:           cfg.BrowserURL,
		CoreAPIKey:           cfg.CoreAPIKey,
		ExaAPIKey:            cfg.ExaAPIKey,
		UnpaywallEmail:       cfg.UnpaywallEmail,
		EnableLawfulFullText: true,
		Translate: func(ctx context.Context, query, lang string) string {
			return a.translate.TranslateQuery(ctx, query, lang)
		},
	})
	a.ingest = &service.LiveIngestor{
		Registry:   registry,
		Sources:    a.sources,
		Articles:   a.articles,
		Translate:  a.translate,
		LocalStore: service.NewLocalStore(cfg.ArticlesDir),
		Enricher: &service.DoiEnrichmentService{
			Articles:    a.articles,
			Mailto:      cfg.CrossrefMailto,
			OpenAlexKey: cfg.OpenAlexAPIKey,
			Logger:      logger,
		},
		Logger: logger,
	}
	return a
}

// SearchJobWorker builds the River worker used by the worker process.
func (a *API) SearchJobWorker() *jobs.SearchJobWorker {
	return &jobs.SearchJobWorker{
		Jobs:      a.jobs,
		Search:    a.search,
		Ingestor:  a.ingest,
		Enricher:  a.enricher,
		Logger:    a.logger,
		Freshness: a.cfg.Search.DefaultFreshnessDays,
	}
}

// Register adds the API routes to the mux.
func (a *API) Register(mux *http.ServeMux, limiter func(http.Handler) http.Handler) {
	handler := HandlerWithOptions(a, StdHTTPServerOptions{
		ErrorHandlerFunc: func(w http.ResponseWriter, r *http.Request, err error) {
			var invalid *InvalidParamFormatError
			if errors.As(err, &invalid) {
				writeDetailError(w, http.StatusNotFound, "No SearchJob matches the given query.")
				return
			}
			writeDetailError(w, http.StatusBadRequest, err.Error())
		},
	})
	throttled := limiter(handler)

	api := http.NewServeMux()
	api.Handle("POST /api/v1/search", throttled)
	api.Handle("POST /api/v1/search/jobs", throttled)
	// The poll endpoint is read-only and exempt from throttling (parity with
	// SearchJobDetailView.throttle_classes = ()).
	api.Handle("GET /api/v1/search/jobs/{job_id}", handler)
	api.Handle("GET /api/v1/source-stats", handler)
	api.Handle("POST /api/v1/admin/reindex", handler)
	mux.Handle("/api/v1/", api)
}

// ---- immediate search -----------------------------------------------------

// SearchArticles runs an immediate search (parity with SearchView.post).
func (a *API) SearchArticles(w http.ResponseWriter, r *http.Request) {
	var req SearchRequest
	if err := decodeJSON(r, &req); err != nil {
		writeDetailError(w, http.StatusBadRequest, "JSON parse error - "+err.Error())
		return
	}
	if items := a.validateSearchRequest(&req); len(items) > 0 {
		writeEnvelope(w, http.StatusBadRequest, "validation_error", items)
		return
	}
	sortBy := ""
	if req.SortBy != nil {
		s := string(*req.SortBy)
		if s != "" && s != "relevance" {
			sortBy = s
		}
	}
	filters := a.filtersFromRequest(req.PeerReviewedOnly, req.IndexedOnly,
		req.ExcludePreprints, req.ExcludeRetracted, req.YearFrom, req.YearTo, sortBy)

	if derefBool(req.ForceRefresh) {
		if _, _, err := a.ingest.IngestQuery(r.Context(), req.Query, service.IngestOptions{}); err != nil {
			a.logger.Warn("force_refresh ingest failed", "error", err)
		}
	}

	hits, _, err := a.search.RunWithQuotes(r.Context(), req.Query, derefStr(req.Expression), filters)
	if err != nil {
		a.serverError(w, "search failed", err)
		return
	}
	if err := a.search.AttachAuthors(r.Context(), hits); err != nil {
		a.serverError(w, "authors failed", err)
		return
	}
	page, perPage := a.pageParams(req.Page, req.PerPage)
	pageHits, pagination := paginateHits(hits, page, perPage)
	pageResults := a.toResults(pageHits)

	writeJSON(w, http.StatusOK, SearchResponse{
		Query:       strptr(req.Query),
		Count:       intptr(pagination.TotalResults),
		Page:        intptr(pagination.Page),
		PerPage:     intptr(pagination.PerPage),
		TotalPages:  intptr(pagination.TotalPages),
		SourceStats: a.sourceStats(r.Context()),
		Results:     &pageResults,
	})
}

// ---- async job creation ----------------------------------------------------

// CreateSearchJob creates or attaches to an active search job (parity with
// SearchJobCreateView.post).
func (a *API) CreateSearchJob(w http.ResponseWriter, r *http.Request) {
	var req SearchJobCreateRequest
	if err := decodeJSON(r, &req); err != nil {
		writeDetailError(w, http.StatusBadRequest, "JSON parse error - "+err.Error())
		return
	}
	if items := a.validateJobCreate(&req); len(items) > 0 {
		writeEnvelope(w, http.StatusBadRequest, "validation_error", items)
		return
	}
	forceRefresh := derefBool(req.ForceRefresh)
	sortBy := ""
	if req.SortBy != nil {
		s := string(*req.SortBy)
		if s != "" && s != "relevance" {
			sortBy = s
		}
	}
	filters := a.filtersFromRequest(req.PeerReviewedOnly, req.IndexedOnly,
		req.ExcludePreprints, req.ExcludeRetracted, req.YearFrom, req.YearTo, sortBy)

	ctx := r.Context()
	keyMaterial := jobKeyMaterial(req.Query, derefStr(req.Expression), forceRefresh, filters)
	lockKey := "search-job-create:" + sha256Hex(keyMaterial)
	pendingKey := "search-job-pending:" + sha256Hex(keyMaterial)

	job := a.findActiveJob(ctx, req.Query, derefStr(req.Expression), forceRefresh, filters)
	attached := job != nil

	if job == nil && a.redis.SetNX(ctx, lockKey, "1", jobLockTTL).Val() {
		jobID := uuid.New()
		a.redis.Set(ctx, pendingKey, jobID.String(), jobLockTTL)
		dbJob := a.buildQueuedJob(jobID, req.Query, derefStr(req.Expression), forceRefresh, filters)
		err := a.createJobWithTask(ctx, dbJob)
		a.redis.Del(ctx, lockKey, pendingKey)
		if err != nil {
			a.serverError(w, "create search job failed", err)
			return
		}
		job = dbJob
	} else if job == nil {
		if pending := a.redis.Get(ctx, pendingKey).Val(); pending != "" {
			if id, err := uuid.Parse(pending); err == nil {
				job = a.buildQueuedJob(id, req.Query, derefStr(req.Expression), forceRefresh, filters)
				attached = true
			}
		}
	}
	if job == nil {
		dbJob := a.buildQueuedJob(uuid.New(), req.Query, derefStr(req.Expression), forceRefresh, filters)
		if err := a.createJobWithTask(ctx, dbJob); err != nil {
			a.serverError(w, "create search job failed", err)
			return
		}
		job = dbJob
	}

	payload := a.jobPayload(ctx, job, nil, nil)
	payload.AttachedToExisting = boolptr(attached)
	payload.SourceStats = a.sourceStats(ctx)
	status := http.StatusAccepted
	if attached {
		status = http.StatusOK
	}
	writeJSON(w, status, payload)
}

// ---- job polling ------------------------------------------------------------

// GetSearchJob returns the current job state, paginated (parity with
// SearchJobDetailView.get).
func (a *API) GetSearchJob(w http.ResponseWriter, r *http.Request, jobID types.UUID, params GetSearchJobParams) {
	if items := a.validatePagination(params.Page, params.PerPage); len(items) > 0 {
		writeEnvelope(w, http.StatusBadRequest, "validation_error", items)
		return
	}
	job, err := a.jobs.GetByID(r.Context(), jobID.String())
	if errors.Is(err, repository.ErrNotFound) {
		writeDetailError(w, http.StatusNotFound, "No SearchJob matches the given query.")
		return
	}
	if err != nil {
		a.serverError(w, "load search job failed", err)
		return
	}
	page, perPage := a.pageParams(params.Page, params.PerPage)
	pageHits, pagination := paginateHits(job.Results, page, perPage)
	pageResults := a.toResults(pageHits)

	payload := a.jobPayload(r.Context(), job, &pageResults, &pagination)
	payload.SourceStats = a.sourceStats(r.Context())
	payload.Count = intptr(pagination.TotalResults)
	writeJSON(w, http.StatusOK, payload)
}

// ---- source stats -----------------------------------------------------------

// GetSourceStats returns aggregated source health (parity with
// SourceStatsView.get).
func (a *API) GetSourceStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, a.sourceStats(r.Context()))
}

// ---- admin reindex ------------------------------------------------------------

// ReindexArticles triggers a full reindex for admins (parity with
// ReindexView.post).
func (a *API) ReindexArticles(w http.ResponseWriter, r *http.Request) {
	if a.cfg.AdminAPIKey == "" {
		writeDetailError(w, http.StatusServiceUnavailable, "Administration endpoint is disabled.")
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-API-Key")), []byte(a.cfg.AdminAPIKey)) != 1 {
		writeDetailError(w, http.StatusForbidden, "You do not have permission to perform this action.")
		return
	}
	var req ReindexRequest
	query := "scientific research"
	if err := decodeJSON(r, &req); err == nil && req.Query != nil && strings.TrimSpace(*req.Query) != "" {
		query = strings.TrimSpace(*req.Query)
	}
	if _, _, err := a.ingest.IngestQuery(r.Context(), query, service.IngestOptions{}); err != nil {
		a.serverError(w, "reindex failed", err)
		return
	}
	taskID := uuid.New()
	writeJSON(w, http.StatusOK, ReindexResponse{
		TaskId: strptr(taskID.String()),
		Status: strptr("queued"),
	})
}

// ---- shared helpers ----------------------------------------------------------

func (a *API) validateSearchRequest(req *SearchRequest) []ErrorItem {
	var items []ErrorItem
	items = append(items, validateQuery(req.Query)...)
	items = append(items, validateExpression(derefStr(req.Expression))...)
	items = append(items, validateYears(req.YearFrom, req.YearTo)...)
	items = append(items, validatePage(req.Page)...)
	items = append(items, validatePerPage(req.PerPage)...)
	if req.SortBy != nil && !validSortBy(string(*req.SortBy)) {
		items = append(items, errItem("sort_by", `"`+string(*req.SortBy)+`" is not a valid choice.`))
	}
	return items
}

func (a *API) validateJobCreate(req *SearchJobCreateRequest) []ErrorItem {
	var items []ErrorItem
	items = append(items, validateQuery(req.Query)...)
	items = append(items, validateExpression(derefStr(req.Expression))...)
	items = append(items, validateYears(req.YearFrom, req.YearTo)...)
	if req.SortBy != nil && !validSortBy(string(*req.SortBy)) {
		items = append(items, errItem("sort_by", `"`+string(*req.SortBy)+`" is not a valid choice.`))
	}
	return items
}

func (a *API) validatePagination(page, perPage *int) []ErrorItem {
	var items []ErrorItem
	items = append(items, validatePage(page)...)
	items = append(items, validatePerPage(perPage)...)
	return items
}

func validateQuery(query string) []ErrorItem {
	if strings.TrimSpace(query) == "" {
		return []ErrorItem{errItem("query", "This field is required.")}
	}
	if len(query) > 512 {
		return []ErrorItem{errItem("query", "Ensure this field has at most 512 characters.")}
	}
	return nil
}

func validateExpression(expression string) []ErrorItem {
	if len(expression) > 1024 {
		return []ErrorItem{errItem("expression", "Ensure this field has at most 1024 characters.")}
	}
	return nil
}

func validateYears(yearFrom, yearTo *int) []ErrorItem {
	var items []ErrorItem
	if yearFrom != nil && *yearFrom < 0 {
		items = append(items, errItem("year_from", "Ensure this value is greater than or equal to 0."))
	}
	if yearFrom != nil && *yearFrom > 9999 {
		items = append(items, errItem("year_from", "Ensure this value is less than or equal to 9999."))
	}
	if yearTo != nil && *yearTo < 0 {
		items = append(items, errItem("year_to", "Ensure this value is greater than or equal to 0."))
	}
	if yearTo != nil && *yearTo > 9999 {
		items = append(items, errItem("year_to", "Ensure this value is less than or equal to 9999."))
	}
	return items
}

func validatePage(page *int) []ErrorItem {
	if page != nil && *page < 1 {
		return []ErrorItem{errItem("page", "Ensure this value is greater than or equal to 1.")}
	}
	return nil
}

func validatePerPage(perPage *int) []ErrorItem {
	if perPage == nil {
		return nil
	}
	if *perPage < 1 {
		return []ErrorItem{errItem("per_page", "Ensure this value is greater than or equal to 1.")}
	}
	if *perPage > 50 {
		return []ErrorItem{errItem("per_page", "Ensure this value is less than or equal to 50.")}
	}
	return nil
}

func validSortBy(s string) bool {
	return s == "relevance" || s == "newest" || s == "metadata"
}

func errItem(attr, detail string) ErrorItem {
	return ErrorItem{Code: strptr("error"), Detail: strptr(detail), Attr: strptr(attr)}
}

func (a *API) filtersFromRequest(peerReviewedOnly, indexedOnly, excludePreprints, excludeRetracted *bool, yearFrom, yearTo *int, sortBy string) domain.SearchFilters {
	return domain.SearchFilters{
		PeerReviewedOnly: derefBool(peerReviewedOnly),
		IndexedOnly:      derefBool(indexedOnly),
		ExcludePreprints: derefBool(excludePreprints),
		ExcludeRetracted: derefBool(excludeRetracted),
		YearFrom:         yearFrom,
		YearTo:           yearTo,
		SortBy:           sortBy,
	}
}

func (a *API) findActiveJob(ctx context.Context, query, expression string, forceRefresh bool, filters domain.SearchFilters) *domain.SearchJob {
	job, err := a.jobs.FindActive(ctx, query, expression, forceRefresh, filters)
	if err != nil {
		a.logger.Warn("find active job failed", "error", err)
		return nil
	}
	return job
}

func (a *API) buildQueuedJob(id uuid.UUID, query, expression string, forceRefresh bool, filters domain.SearchFilters) *domain.SearchJob {
	now := time.Now()
	sub := domain.StageSubstage["queued"]
	return &domain.SearchJob{
		ID:                    id.String(),
		Query:                 query,
		Expression:            expression,
		ForceRefreshRequested: forceRefresh,
		FreshnessDaysUsed:     a.cfg.Search.DefaultFreshnessDays,
		Status:                domain.JobStatusQueued,
		Stage:                 "queued",
		Substage:              sub[0],
		SubstageLabel:         sub[1],
		Message:               "Задача поставлена в очередь",
		SourceFailed:          []string{},
		SourceTimings:         map[string]domain.SourceTiming{},
		Results:               []domain.SearchHit{},
		Filters:               filters,
		CreatedAt:             now,
		UpdatedAt:             now,
	}
}

func (a *API) createJobWithTask(ctx context.Context, dbJob *domain.SearchJob) error {
	tx, err := a.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if err := a.jobs.CreateTx(ctx, tx, dbJob); err != nil {
		return err
	}
	if _, err := a.river.InsertTx(ctx, tx, &jobs.SearchJobTask{JobID: dbJob.ID}, nil); err != nil {
		return fmt.Errorf("insert river task: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit tx: %w", err)
	}
	return nil
}

func (a *API) sourceStats(ctx context.Context) *SourceStats {
	if cached, err := a.redis.Get(ctx, sourceStatsCacheKey).Result(); err == nil {
		var stats SourceStats
		if json.Unmarshal([]byte(cached), &stats) == nil {
			return &stats
		}
	}
	total, failed, live, err := a.stats.Compute(ctx)
	if err != nil {
		a.logger.Warn("compute source stats failed", "error", err)
		return &SourceStats{Total: intptr(0), Live: intptr(0), Failed: &[]string{}}
	}
	stats := &SourceStats{Total: intptr(total), Live: intptr(live), Failed: &failed}
	if payload, err := json.Marshal(stats); err == nil {
		a.redis.Set(ctx, sourceStatsCacheKey, payload, sourceStatsTTL)
	}
	return stats
}

func (a *API) pageParams(page, perPage *int) (int, int) {
	p, pp := 1, 5
	if page != nil && *page >= 1 {
		p = *page
	}
	if perPage != nil && *perPage >= 1 && *perPage <= 50 {
		pp = *perPage
	}
	return p, pp
}

func (a *API) jobPayload(ctx context.Context, job *domain.SearchJob, results *[]SearchResult, pagination *Pagination) *SearchJobPayload {
	waitStats, err := a.jobs.WaitStats(ctx)
	if err != nil {
		a.logger.Warn("load wait stats failed", "error", err)
		waitStats = map[string]domain.SearchWaitStat{}
	}
	var (
		withoutEnrichment *int
		withEnrichment    *int
	)
	if s, ok := waitStats[domain.WaitStatWithoutEnrichment]; ok && s.SampleCount > 0 {
		withoutEnrichment = intptr(roundHalfUp(s.AverageSeconds))
	}
	if s, ok := waitStats[domain.WaitStatWithEnrichment]; ok && s.SampleCount > 0 {
		withEnrichment = intptr(roundHalfUp(s.AverageSeconds))
	}

	id := uuid.MustParse(job.ID)
	payload := &SearchJobPayload{
		Id:                                  &id,
		Query:                               strptr(job.Query),
		Expression:                          strptr(job.Expression),
		Status:                              strptr(job.Status),
		Stage:                               strptr(job.Stage),
		Substage:                            strptr(job.Substage),
		SubstageLabel:                       strptr(job.SubstageLabel),
		Message:                             strptr(job.Message),
		ProgressPercent:                     intptr(progressPercent(job)),
		SourceTotal:                         intptr(job.SourceTotal),
		SourceDone:                          intptr(job.SourceDone),
		SourceLive:                          intptr(job.SourceLive),
		SourceFailed:                        &job.SourceFailed,
		SourceTimings:                       a.sourceTimings(job.SourceTimings),
		AverageWaitWithoutEnrichmentSeconds: withoutEnrichment,
		AverageWaitWithEnrichmentSeconds:    withEnrichment,
		IndexHitsBefore:                     intptr(job.IndexHitsBefore),
		IndexHitsAfter:                      intptr(job.IndexHitsAfter),
		RescanTriggered:                     boolptr(job.RescanTriggered),
		RescanReason:                        strptr(job.RescanReason),
		FreshnessDaysUsed:                   intptr(job.FreshnessDaysUsed),
		PeerReviewedOnly:                    boolptr(job.Filters.PeerReviewedOnly),
		IndexedOnly:                         boolptr(job.Filters.IndexedOnly),
		ExcludePreprints:                    boolptr(job.Filters.ExcludePreprints),
		ExcludeRetracted:                    boolptr(job.Filters.ExcludeRetracted),
		YearFrom:                            job.Filters.YearFrom,
		YearTo:                              job.Filters.YearTo,
		SortBy:                              strptr(job.Filters.NormalizedSort()),
		CreatedAt:                           &job.CreatedAt,
		UpdatedAt:                           &job.UpdatedAt,
		FinishedAt:                          job.FinishedAt,
		Error:                               strptr(job.Error),
		Results:                             results,
	}
	if pagination != nil {
		payload.Page = intptr(pagination.Page)
		payload.PerPage = intptr(pagination.PerPage)
		payload.TotalPages = intptr(pagination.TotalPages)
		payload.TotalResults = intptr(pagination.TotalResults)
	}
	return payload
}

func (a *API) sourceTimings(timings map[string]domain.SourceTiming) *map[string]SourceTiming {
	if timings == nil {
		timings = map[string]domain.SourceTiming{}
	}
	out := make(map[string]SourceTiming, len(timings))
	for key, t := range timings {
		out[key] = SourceTiming{
			Status:        strptr(t.Status),
			FetchSeconds:  &t.FetchSeconds,
			EnrichSeconds: &t.EnrichSeconds,
			SaveSeconds:   &t.SaveSeconds,
			TotalSeconds:  &t.TotalSeconds,
			ArticlesCount: intptr(t.ArticlesCount),
		}
	}
	return &out
}

// toResults converts domain hits into the generated schema.
func (a *API) toResults(hits []domain.SearchHit) []SearchResult {
	results := make([]SearchResult, 0, len(hits))
	for _, hit := range hits {
		authors := hit.Authors
		if authors == nil {
			authors = []string{}
		}
		identifiers := hit.Identifiers
		if identifiers == nil {
			identifiers = map[string]string{}
		}
		quotes := make([]Quote, 0, len(hit.Quotes))
		for _, q := range hit.Quotes {
			quotes = append(quotes, Quote{
				Text:      strptr(q.Text),
				Location:  strptr(q.Location),
				Relevance: &q.Relevance,
				Rationale: strptr(q.Rationale),
			})
		}
		var publicationDate *types.Date
		if hit.PublicationDate != nil {
			d := types.Date{Time: *hit.PublicationDate}
			publicationDate = &d
		}
		results = append(results, SearchResult{
			Id:              int64ptr(hit.ID),
			Title:           strptr(hit.Title),
			Preview:         strptr(hit.Preview),
			Year:            hit.Year,
			PublicationDate: publicationDate,
			Source:          strptr(hit.Source),
			Journal:         strptr(hit.Journal),
			Authors:         &authors,
			Volume:          strptr(hit.Volume),
			Issue:           strptr(hit.Issue),
			Pages:           strptr(hit.Pages),
			Doi:             strptr(hit.DOI),
			Identifiers:     &identifiers,
			EligibilityEvidence: &EligibilityEvidence{
				PeerReviewed:      boolptr(hit.IsPeerReviewed),
				Indexed:           boolptr(hit.Indexed),
				DoiAndJournalCard: boolptr(hit.DOIAndCard),
				NotPreprint:       boolptr(hit.NotPreprint),
			},
			EligibilityConfidence: &EligibilityConfidence{
				PeerReviewed:      &hit.PeerReviewConf,
				Indexed:           &hit.IndexingConf,
				DoiAndJournalCard: &hit.DOIAndCardConf,
				NotPreprint:       &hit.NotPreprintConf,
				Overall:           &hit.OverallConf,
			},
			IsRetracted:    boolptr(hit.IsRetracted),
			RetractionNote: strptr(hit.RetractionNote),
			CitedByCount:   intptr(hit.CitedByCount),
			Tier:           strptr(hit.Tier),
			Url:            strptr(hit.URL),
			RerankScore:    &hit.RerankScore,
			Quotes:         &quotes,
			Tldr:           strptr(hit.TLDR),
		})
	}
	return results
}

// Pagination holds page metadata for job payloads.
type Pagination struct {
	Page         int
	PerPage      int
	TotalPages   int
	TotalResults int
}

func (a *API) serverError(w http.ResponseWriter, message string, err error) {
	a.logger.Error(message, "error", err)
	writeDetailError(w, http.StatusInternalServerError, "Internal server error")
}

func decodeJSON(r *http.Request, dst any) error {
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	if err := dec.Decode(dst); err != nil {
		return err
	}
	return nil
}

func paginateHits(hits []domain.SearchHit, page, perPage int) ([]domain.SearchHit, Pagination) {
	total := len(hits)
	totalPages := int(math.Ceil(float64(total) / float64(perPage)))
	start := (page - 1) * perPage
	end := start + perPage
	if start > total {
		start = total
	}
	if end > total {
		end = total
	}
	return hits[start:end], Pagination{
		Page: page, PerPage: perPage, TotalPages: totalPages, TotalResults: total,
	}
}

func progressPercent(job *domain.SearchJob) int {
	switch job.Status {
	case domain.JobStatusCompleted, domain.JobStatusPartial, domain.JobStatusFailed:
		return 100
	}
	if job.Stage == "live_scan" && job.SourceTotal > 0 {
		base := domain.StageProgress["checking_index"]
		span := domain.StageProgress["live_scan"] - base
		phaseRatio := domain.LiveScanPhaseRatio[job.Substage]
		done := job.SourceDone
		if done > job.SourceTotal {
			done = job.SourceTotal
		}
		value := base + int((float64(done)+phaseRatio)/float64(job.SourceTotal)*float64(span))
		if value > 80 {
			return 80
		}
		return value
	}
	if v, ok := domain.StageProgress[job.Stage]; ok {
		return v
	}
	return 10
}

func roundHalfUp(v float64) int {
	return int(v + 0.5)
}

func jobKeyMaterial(query, expression string, forceRefresh bool, filters domain.SearchFilters) string {
	return strings.Join([]string{
		normalizeJobText(query),
		normalizeJobText(expression),
		"0", // force_refresh is always false in stored jobs (parity)
		boolInt(forceRefresh),
		filters.Signature(),
	}, "|")
}

func boolInt(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

func normalizeJobText(s string) string {
	return strings.ToLower(strings.Join(strings.Fields(s), " "))
}

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func derefStr(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func derefBool(p *bool) bool {
	if p == nil {
		return false
	}
	return *p
}

func boolptr(b bool) *bool { return &b }

func int64ptr(n int64) *int64 { return &n }
