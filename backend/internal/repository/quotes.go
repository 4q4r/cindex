package repository

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Quotes stores the per-article PERELMAN cache rows (extraction_articlequotes).
type Quotes struct {
	pool *pgxpool.Pool
}

// NewQuotes builds the PERELMAN cache repository over the given pool.
func NewQuotes(pool *pgxpool.Pool) *Quotes {
	return &Quotes{pool: pool}
}

// GetByArticle returns the cache row for an article, or ErrNotFound.
func (r *Quotes) GetByArticle(ctx context.Context, articleID int64) (*domain.ArticleQuotes, error) {
	var q domain.ArticleQuotes
	var quotesJSON []byte
	err := r.pool.QueryRow(ctx, `
		SELECT article_id, quotes, tldr, status, extracted_at, model, error,
		       created_at, updated_at
		FROM extraction_articlequotes WHERE article_id = $1`, articleID).Scan(
		&q.ArticleID, &quotesJSON, &q.TLDR, &q.Status, &q.ExtractedAt, &q.Model,
		&q.Error, &q.CreatedAt, &q.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("get quotes row: %w", err)
	}
	if len(quotesJSON) > 0 {
		if err := json.Unmarshal(quotesJSON, &q.Quotes); err != nil {
			return nil, fmt.Errorf("unmarshal quotes: %w", err)
		}
	}
	return &q, nil
}

// Claim acquires the concurrent-extraction lock row (get_or_create with
// status=pending, parity with the Django claim). Returns whether this caller
// won the claim. Idempotent: an existing done row returns (row, false) so the
// cache hit path reuses the persisted result.
func (r *Quotes) Claim(ctx context.Context, articleID int64) (*domain.ArticleQuotes, bool, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return nil, false, fmt.Errorf("begin claim tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	row, err := r.claimRow(ctx, tx, articleID)
	if errors.Is(err, pgx.ErrNoRows) {
		tag, err := tx.Exec(ctx, `
			INSERT INTO extraction_articlequotes (article_id, status, created_at, updated_at)
			VALUES ($1, 'pending', $2, $2)
			ON CONFLICT (article_id) DO NOTHING`, articleID, time.Now())
		if err != nil {
			return nil, false, fmt.Errorf("insert claim: %w", err)
		}
		if tag.RowsAffected() == 0 {
			// Lost the race: the concurrent insert just committed; load it.
			row, err = r.claimRow(ctx, tx, articleID)
			if err != nil {
				return nil, false, fmt.Errorf("load raced claim: %w", err)
			}
			if err := tx.Commit(ctx); err != nil {
				return nil, false, fmt.Errorf("commit claim tx: %w", err)
			}
			return row, false, nil
		}
		if err := tx.Commit(ctx); err != nil {
			return nil, false, fmt.Errorf("commit claim tx: %w", err)
		}
		return &domain.ArticleQuotes{ArticleID: articleID, Status: domain.QuotesStatusPending}, true, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("lock quotes row: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, false, fmt.Errorf("commit claim tx: %w", err)
	}
	return row, false, nil
}

func (r *Quotes) claimRow(ctx context.Context, tx pgx.Tx, articleID int64) (*domain.ArticleQuotes, error) {
	var q domain.ArticleQuotes
	var quotesJSON []byte
	err := tx.QueryRow(ctx, `
		SELECT article_id, quotes, tldr, status, extracted_at, model, error,
		       created_at, updated_at
		FROM extraction_articlequotes WHERE article_id = $1
		FOR UPDATE`, articleID).Scan(
		&q.ArticleID, &quotesJSON, &q.TLDR, &q.Status, &q.ExtractedAt, &q.Model,
		&q.Error, &q.CreatedAt, &q.UpdatedAt,
	)
	if err != nil {
		return nil, err
	}
	if len(quotesJSON) > 0 {
		if err := json.Unmarshal(quotesJSON, &q.Quotes); err != nil {
			return nil, fmt.Errorf("unmarshal quotes: %w", err)
		}
	}
	return &q, nil
}

// SetDone persists a successful extraction (quotes + tldr).
func (r *Quotes) SetDone(ctx context.Context, articleID int64, quotes []domain.Quote, tldr, model string) error {
	payload, err := json.Marshal(quotes)
	if err != nil {
		return fmt.Errorf("marshal quotes: %w", err)
	}
	if payload == nil {
		payload = []byte("[]")
	}
	now := time.Now()
	_, err = r.pool.Exec(ctx, `
		INSERT INTO extraction_articlequotes (article_id, quotes, tldr, status, extracted_at, model, created_at, updated_at)
		VALUES ($1, $2, $3, 'done', $4, $5, $6, $6)
		ON CONFLICT (article_id) DO UPDATE SET
			quotes = EXCLUDED.quotes, tldr = EXCLUDED.tldr, status = 'done',
			extracted_at = EXCLUDED.extracted_at, model = EXCLUDED.model,
			updated_at = EXCLUDED.updated_at`,
		articleID, payload, tldr, now, model, now)
	if err != nil {
		return fmt.Errorf("set done: %w", err)
	}
	return nil
}

// SetNoText persists the "no extractable text" outcome.
func (r *Quotes) SetNoText(ctx context.Context, articleID int64, model string) error {
	return r.setOutcome(ctx, articleID, domain.QuotesStatusNoText, model, "")
}

// SetFailed persists a failed extraction with an error message.
func (r *Quotes) SetFailed(ctx context.Context, articleID int64, model, errMsg string) error {
	return r.setOutcome(ctx, articleID, domain.QuotesStatusFailed, model, errMsg)
}

func (r *Quotes) setOutcome(ctx context.Context, articleID int64, status, model, errMsg string) error {
	now := time.Now()
	_, err := r.pool.Exec(ctx, `
		INSERT INTO extraction_articlequotes (article_id, status, model, error, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $5)
		ON CONFLICT (article_id) DO UPDATE SET
			status = EXCLUDED.status, model = EXCLUDED.model, error = EXCLUDED.error,
			extracted_at = NULL, updated_at = EXCLUDED.updated_at`,
		articleID, status, model, errMsg, now)
	if err != nil {
		return fmt.Errorf("set outcome %s: %w", status, err)
	}
	return nil
}
