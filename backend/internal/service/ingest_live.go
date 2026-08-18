package service

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/4q4r/cindex/backend/internal/connector"
	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
)

const liveSourceTimeout = 45 * time.Second

// LiveIngestor runs corpus live scans through the real connector registry,
// with full parity to IngestorService in apps.ingestion.services:
// per-source translate → fetch → enrich (local-md first) → save → DOI
// backfill, plus circuit-breaker source bookkeeping.
type LiveIngestor struct {
	Registry   *connector.Registry
	Sources    *repository.Sources
	Articles   *repository.Articles
	Translate  *Translate
	LocalStore *LocalStore
	Enricher   *DoiEnrichmentService
	Logger     *slog.Logger
}

// SourceHealth returns the key→status map (parity with
// IngestorService.source_health: healthy / circuit_open / never_succeeded /
// never_queried, computed over the canonical connector keys).
func (l *LiveIngestor) SourceHealth(ctx context.Context) (map[string]string, error) {
	health := make(map[string]string, len(l.Registry.Keys()))
	for _, key := range l.Registry.Keys() {
		src, err := l.Sources.GetByKey(ctx, key)
		if err != nil {
			if errors.Is(err, repository.ErrNotFound) {
				health[key] = "never_queried"
				continue
			}
			return nil, err
		}
		if isCircuitOpen(src) {
			health[key] = "circuit_open"
		} else if src.LastSuccessAt == nil && src.TotalRuns > 0 {
			health[key] = "never_succeeded"
		} else {
			health[key] = "healthy"
		}
	}
	return health, nil
}

// SourceNames returns a key→display-name map for failed-source reporting
// (parity with source_health's name fallback).
func (l *LiveIngestor) SourceNames(ctx context.Context) (map[string]string, error) {
	names := make(map[string]string)
	for _, key := range l.Registry.Keys() {
		src, err := l.Sources.GetByKey(ctx, key)
		if err != nil {
			if errors.Is(err, repository.ErrNotFound) {
				continue
			}
			return nil, err
		}
		if src.Name != "" {
			names[key] = src.Name
		}
	}
	return names, nil
}

func isCircuitOpen(s *domain.Source) bool {
	return s.CircuitOpenUntil != nil && s.CircuitOpenUntil.After(time.Now())
}

// IngestQuery runs a live scan for query across the selected sources.
// Returns (completedKeys, failedSources, error) — parity with
// IngestorService.ingest_query.
func (l *LiveIngestor) IngestQuery(ctx context.Context, query string, opts IngestOptions) ([]string, []string, error) {
	if l.Registry == nil {
		return nil, nil, errors.New("live ingestor: connector registry is nil")
	}

	selected := opts.SourceKeys
	if len(selected) == 0 {
		selected = l.Registry.Keys()
	}
	resumed := make(map[string]bool, len(opts.ResumeCompletedKeys))
	for _, k := range opts.ResumeCompletedKeys {
		resumed[k] = true
	}
	failedSources := append([]string(nil), opts.InitialFailed...)
	completedKeys := append([]string(nil), opts.ResumeCompletedKeys...)
	done := opts.InitialDone
	if done < 0 {
		done = 0
	}
	resumedSelected := 0
	for _, key := range selected {
		if resumed[key] {
			resumedSelected++
		}
	}
	if done < resumedSelected {
		done = resumedSelected
	}
	total := len(selected)

	if opts.Progress != nil {
		substage := "queued"
		label := "Запрос принят"
		if done > 0 {
			substage = "resuming"
			label = "Возобновляем поиск после рестарта"
		}
		opts.Progress(ProgressEvent{
			Substage: substage, SubstageLabel: label,
			DoneSources: done, TotalSources: total, FailedSources: failedSources,
		})
	}

	var saved []EnrichCandidate
	for _, sourceKey := range selected {
		if resumed[sourceKey] {
			continue
		}
		conn, ok := l.Registry.Get(sourceKey)
		if !ok {
			done++
			continue
		}
		src, err := l.Sources.GetByKey(ctx, sourceKey)
		if errors.Is(err, repository.ErrNotFound) {
			srcID, err2 := l.Sources.EnsureExists(ctx, sourceKey, upsertSourceName(sourceKey), upsertSourceBaseURL(sourceKey))
			if err2 != nil {
				return nil, failedSources, err2
			}
			src = &domain.Source{
				ID: srcID, Key: sourceKey, Name: upsertSourceName(sourceKey),
				BaseURL: upsertSourceBaseURL(sourceKey), Active: true,
			}
		} else if err != nil {
			return nil, failedSources, err
		}
		if isCircuitOpen(src) {
			failedSources = append(failedSources, sourceName(src, sourceKey))
			done++
			if opts.Profile != nil {
				opts.Profile(ProfileEvent{
					SourceKey: sourceKey, SourceName: src.Name,
					Status: "skipped", StartTime: time.Now(), EndTime: time.Now(),
				})
			}
			if opts.Progress != nil {
				opts.Progress(ProgressEvent{
					Substage: "source_skipped", SubstageLabel: "Источник пропущен",
					DoneSources: done, TotalSources: total, FailedSources: failedSources,
					TimingSource: sourceName(src, sourceKey),
				})
			}
			completedKeys = append(completedKeys, sourceKey)
			continue
		}

		sourceSaved, newDone, newFailed, err := l.processSource(ctx, conn, src, sourceKey, query, opts, done, total, failedSources)
		if err != nil {
			return completedKeys, newFailed, err
		}
		saved = append(saved, sourceSaved...)
		done = newDone
		failedSources = newFailed
		completedKeys = append(completedKeys, sourceKey)
	}

	if len(saved) > 0 && l.Enricher != nil {
		l.Enricher.Enrich(ctx, saved)
	}
	return completedKeys, failedSources, nil
}

// processSource is the per-source pipeline: fetch → enrich (local-md first,
// degrade to raw on failure, drop on nil) → mark success → DOI-gated save.
func (l *LiveIngestor) processSource(
	ctx context.Context,
	conn connector.Connector,
	src *domain.Source,
	sourceKey, query string,
	opts IngestOptions,
	done, total int,
	failedSources []string,
) ([]EnrichCandidate, int, []string, error) {
	sourceCtx, cancel := context.WithTimeout(ctx, liveSourceTimeout)
	defer cancel()
	logger := l.logger()
	sourceStarted := time.Now()

	emitProgress := func(substage, label string) {
		if opts.Progress != nil {
			opts.Progress(ProgressEvent{
				Substage: substage, SubstageLabel: label,
				DoneSources: done, TotalSources: total, FailedSources: failedSources,
				TimingSource: sourceName(src, sourceKey),
			})
		}
	}

	emitProgress("fetching", "Собираем статьи")
	sourceQuery := query
	if l.Translate != nil {
		sourceQuery = l.Translate.TranslateQueryForSource(sourceCtx, query, sourceKey)
	}
	raws, err := conn.Fetch(sourceCtx, sourceQuery, opts.PerSourceLimit)
	if err != nil {
		if recordErr := l.markFailure(ctx, src, err); recordErr != nil {
			return nil, done, failedSources, recordErr
		}
		failedSources = append(failedSources, sourceName(src, sourceKey))
		done++
		if opts.Profile != nil {
			opts.Profile(ProfileEvent{
				SourceKey: sourceKey, SourceName: src.Name, Status: "failed",
				StartTime: sourceStarted, EndTime: time.Now(), ArticlesCount: 0,
			})
		}
		emitProgress("failed", "Источник не ответил")
		return nil, done, failedSources, nil
	}
	emitProgress("enriching", "Обогащаем карточки")
	var enriched []connector.RawArticle
	for _, raw := range raws {
		enrichedRaw, drop := l.enrichOne(sourceCtx, conn, raw)
		if drop {
			continue
		}
		enriched = append(enriched, enrichedRaw)
	}
	if err := l.markSuccess(ctx, src); err != nil {
		return nil, done, failedSources, err
	}

	emitProgress("indexing", "Индексируем статьи")
	var saved []EnrichCandidate
	for _, raw := range enriched {
		if raw.DOI == "" || !hasDOI10Prefix(raw.DOI) {
			logger.Warn("ingestion: dropping article without valid DOI",
				"source_key", sourceKey, "url", raw.URL, "title", truncateTitle(raw.Title))
			continue
		}
		article, authors, err := l.saveArticle(sourceCtx, raw)
		if err != nil {
			return nil, done, failedSources, err
		}
		saved = append(saved, EnrichCandidate{Article: article, Authors: authors})
	}
	done++
	if opts.Profile != nil {
		opts.Profile(ProfileEvent{
			SourceKey: sourceKey, SourceName: src.Name, Status: "completed",
			StartTime: sourceStarted, EndTime: time.Now(), ArticlesCount: len(saved),
		})
	}
	emitProgress("completed", "Источник обработан")
	return saved, done, failedSources, nil
}

// enrichOne applies the local-md-first enrichment path, degrading to the raw
// payload on failure and dropping the record on a nil enrichment (parity with
// the per-raw loop in _process_single_source).
func (l *LiveIngestor) enrichOne(ctx context.Context, conn connector.Connector, raw connector.RawArticle) (connector.RawArticle, bool) {
	var enriched *connector.RawArticle
	var err error
	if l.LocalStore != nil && l.LocalStore.Exists(raw.DOI) {
		enriched = l.LocalStore.ToRaw(raw.DOI, raw)
		if enriched == nil {
			enriched, err = conn.EnrichRaw(ctx, raw)
		} else {
			l.logger().Info("ingestion: local-md hit, skipping network enrich", "doi", raw.DOI)
		}
	} else {
		enriched, err = conn.EnrichRaw(ctx, raw)
	}
	if err != nil {
		l.logger().Warn("ingestion: enrich_raw failed, keeping raw payload",
			"url", raw.URL, "error", err)
		return raw, false
	}
	if enriched == nil {
		l.logger().Info("ingestion: enrich_raw dropped non-article record", "url", raw.URL)
		return connector.RawArticle{}, true
	}
	return *enriched, false
}

// saveArticle persists a DOI-valid raw article with journal, identifier,
// author and eligibility bookkeeping (parity with _save_article).
func (l *LiveIngestor) saveArticle(ctx context.Context, raw connector.RawArticle) (*domain.Article, []string, error) {
	src, err := l.Sources.GetByKey(ctx, raw.SourceKey)
	if errors.Is(err, repository.ErrNotFound) {
		if _, err2 := l.Sources.EnsureExists(ctx, raw.SourceKey, upsertSourceName(raw.SourceKey), upsertSourceBaseURL(raw.SourceKey)); err2 != nil {
			l.logger().Error("ingestion: ensure source failed", "source_key", raw.SourceKey, "error", err2)
			return nil, nil, err2
		}
		src, err = l.Sources.GetByKey(ctx, raw.SourceKey)
	}
	if err != nil {
		l.logger().Error("ingestion: load source failed", "source_key", raw.SourceKey, "error", err)
		return nil, nil, err
	}

	journalName := raw.Journal
	if journalName == "" {
		journalName = raw.SourceKey
	}
	journalID, err := l.Sources.UpsertJournal(ctx, journalName)
	if err != nil {
		l.logger().Error("ingestion: upsert journal failed", "journal", journalName, "error", err)
		return nil, nil, err
	}

	var parsed []string
	for _, name := range raw.Authors {
		if trimmed := strings.TrimSpace(name); trimmed != "" {
			parsed = append(parsed, trimmed)
		}
	}
	article := &domain.Article{
		SourceID:           src.ID,
		JournalID:          &journalID,
		Title:              raw.Title,
		Abstract:           raw.Abstract,
		FullText:           raw.FullText,
		Language:           raw.Language,
		PubYear:            raw.Year,
		URL:                raw.URL,
		DOI:                raw.DOI,
		Volume:             raw.Volume,
		Issue:              raw.Issue,
		Pages:              raw.Pages,
		PeerReviewEvidence: raw.PeerReviewEvidence,
		IndexingEvidence:   raw.IndexingEvidence,
		PreprintEvidence:   raw.PreprintEvidence,
		IsRetracted:        raw.IsRetracted,
		RetractionNote:     raw.RetractionNote,
		CitedByCount:       raw.CitedByCount,
	}

	// Retraction OR-logic: a connector unaware of a retraction must never
	// clear a flag another source already set.
	var persistedAuthors []string
	if existing, err2 := l.Articles.GetByDOI(ctx, raw.DOI); err2 == nil {
		article.IsRetracted = article.IsRetracted || existing.IsRetracted
		if article.RetractionNote == "" {
			article.RetractionNote = existing.RetractionNote
		}
		if len(parsed) == 0 {
			persistedAuthors, err = l.Articles.GetAuthors(ctx, existing.ID)
			if err != nil {
				return nil, nil, fmt.Errorf("load existing article authors: %w", err)
			}
		}
	} else if !errors.Is(err2, repository.ErrNotFound) {
		return nil, nil, fmt.Errorf("load existing article: %w", err2)
	}

	authors := parsed
	candidateAuthors := parsed
	if len(authors) == 0 {
		if len(persistedAuthors) > 0 {
			authors = nil // keep persisted authors
			candidateAuthors = persistedAuthors
		} else {
			authors = []string{"Unknown author"}
			candidateAuthors = authors
		}
	}

	eligibility := domain.ApplyEligibility(article, raw.SourceKey, journalName)
	id, err := l.Articles.SaveIngested(ctx, article, authors, eligibility)
	if err != nil {
		return nil, nil, fmt.Errorf("save ingested article: %w", err)
	}
	article.ID = id
	return article, candidateAuthors, nil
}

func (l *LiveIngestor) markSuccess(ctx context.Context, src *domain.Source) error {
	return l.Sources.RecordRun(ctx, src.ID, repository.SourceRunOutcome{Success: true})
}

func (l *LiveIngestor) markFailure(ctx context.Context, src *domain.Source, runErr error) error {
	msg := ""
	if runErr != nil {
		msg = truncateError(runErr.Error())
	}
	outcome := repository.SourceRunOutcome{
		Success: false,
		Error:   msg,
	}
	return l.Sources.RecordRun(ctx, src.ID, outcome)
}

func (l *LiveIngestor) logger() *slog.Logger {
	if l.Logger != nil {
		return l.Logger
	}
	return slog.Default()
}

func upsertSourceName(key string) string {
	if key == "local_import" {
		return "LOCAL IMPORT"
	}
	return strings.ToUpper(key)
}

func upsertSourceBaseURL(key string) string {
	if key == "local_import" {
		return "https://local-import.invalid"
	}
	return "https://" + key + ".org"
}

func sourceName(src *domain.Source, key string) string {
	if src != nil && src.Name != "" {
		return src.Name
	}
	return strings.ToUpper(key)
}

func hasDOI10Prefix(doi string) bool {
	return strings.HasPrefix(doi, "10.")
}

func truncateTitle(title string) string {
	if len(title) > 120 {
		return title[:120]
	}
	return title
}

func truncateError(msg string) string {
	if runes := []rune(msg); len(runes) > 2000 {
		return string(runes[:2000])
	}
	return msg
}
