package service

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
)

// QuoteExtractor fills search hits with PERELMAN quotes and TLDRs.
type QuoteExtractor interface {
	Enrich(ctx context.Context, hits []domain.SearchHit) error
}

// CacheQuoteExtractor fills quotes/tldr from the ArticleQuotes cache when the
// cached extraction is complete (parity with QuoteExtractionService.enrich's
// cache-aware path).
type CacheQuoteExtractor struct {
	Quotes *repository.Quotes
}

// Enrich applies cached quotes to each hit.
func (c *CacheQuoteExtractor) Enrich(ctx context.Context, hits []domain.SearchHit) error {
	for i := range hits {
		cache, err := c.Quotes.GetByArticle(ctx, hits[i].ID)
		if errors.Is(err, repository.ErrNotFound) {
			continue
		}
		if err != nil {
			return err
		}
		if cache.Status != domain.QuotesStatusDone {
			continue
		}
		hits[i].Quotes = cache.Quotes
		hits[i].TLDR = cache.TLDR
	}
	return nil
}

// PerelmanQuoteExtractor serves completed cache rows and extracts uncached
// results with the query-agnostic PERELMAN client. Published articles are
// claimed, frozen, and cached; preprints are extracted without persistence.
type PerelmanQuoteExtractor struct {
	Articles    *repository.Articles
	Quotes      *repository.Quotes
	Sources     *repository.Sources
	Perelman    *Perelman
	LocalStore  *LocalStore
	Model       string
	Concurrency int
	Logger      *slog.Logger
}

// Enrich applies cache hits immediately and processes uncached articles with
// bounded concurrency. A failure is isolated to its article and recorded on
// the claim row, matching the Django facade's never-abort-batch behavior.
func (e *PerelmanQuoteExtractor) Enrich(ctx context.Context, hits []domain.SearchHit) error {
	if e == nil || e.Perelman == nil {
		return errors.New("perelman extractor is not configured")
	}
	concurrency := e.Concurrency
	if concurrency <= 0 {
		concurrency = 4
	}
	sem := make(chan struct{}, concurrency)
	var wg sync.WaitGroup
	for i := range hits {
		cache, err := e.Quotes.GetByArticle(ctx, hits[i].ID)
		if err == nil && cache.Status == domain.QuotesStatusDone {
			hits[i].Quotes = cache.Quotes
			hits[i].TLDR = cache.TLDR
			continue
		}
		if err != nil && !errors.Is(err, repository.ErrNotFound) {
			return err
		}
		article, err := e.Articles.GetByID(ctx, hits[i].ID)
		if errors.Is(err, repository.ErrNotFound) {
			continue
		}
		if err != nil {
			return err
		}
		source, err := e.Sources.GetByID(ctx, article.SourceID)
		if err != nil {
			return err
		}
		published := article.IsNotPreprintOrManuscript
		if published {
			row, won, claimErr := e.Quotes.Claim(ctx, article.ID)
			if claimErr != nil {
				return claimErr
			}
			if !won {
				if row.Status == domain.QuotesStatusDone {
					hits[i].Quotes = row.Quotes
					hits[i].TLDR = row.TLDR
				}
				continue
			}
		}
		index := i
		hit := hits[i]
		wg.Add(1)
		go func() {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
				defer func() { <-sem }()
			case <-ctx.Done():
				e.fail(article.ID, published, ctx.Err())
				return
			}
			e.extractOne(ctx, &hits[index], hit, article, source.Key, published)
		}()
	}
	wg.Wait()
	return ctx.Err()
}

func (e *PerelmanQuoteExtractor) extractOne(ctx context.Context, hit *domain.SearchHit, metadata domain.SearchHit, article *domain.Article, sourceKey string, published bool) {
	result, err := e.Perelman.Extract(ctx, *article)
	if err != nil {
		e.fail(article.ID, published, err)
		return
	}
	hit.Quotes = result.Quotes
	hit.TLDR = result.TLDR
	if !published {
		return
	}
	if result.IsEmpty() {
		if err := e.Quotes.SetNoText(ctx, article.ID, e.Model); err != nil {
			e.log().Error("perelman: mark no_text failed", "article_id", article.ID, "error", err)
		}
		return
	}
	path, err := e.LocalStore.Save(article, sourceKey, metadata.Journal, metadata.Authors, result)
	if err == nil {
		err = e.Quotes.SetDoneFrozen(ctx, article.ID, path, result.Quotes, result.TLDR, e.Model)
	}
	if err != nil {
		e.fail(article.ID, true, err)
	}
}

func (e *PerelmanQuoteExtractor) fail(articleID int64, published bool, extractionErr error) {
	e.log().Warn("perelman: article extraction failed", "article_id", articleID, "error", extractionErr)
	if published {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := e.Quotes.SetFailed(cleanupCtx, articleID, e.Model, extractionErr.Error()); err != nil {
			e.log().Error("perelman: mark failed failed", "article_id", articleID, "error", err)
		}
	}
}

func (e *PerelmanQuoteExtractor) log() *slog.Logger {
	if e.Logger != nil {
		return e.Logger
	}
	return slog.Default()
}
