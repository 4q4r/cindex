package service

import (
	"context"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
)

// QuoteExtractor fills search hits with PERELMAN quotes and TLDRs. The
// stage-2 implementation is cache-only: it reads the ArticleQuotes cache
// (populated by the Django stack) and never calls the LLM. Stage 5 adds the
// real PERELMAN extraction worker.
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
		if err != nil || cache.Status != domain.QuotesStatusDone {
			continue
		}
		hits[i].Quotes = cache.Quotes
		hits[i].TLDR = cache.TLDR
	}
	return nil
}
