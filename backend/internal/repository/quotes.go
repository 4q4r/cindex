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

// Claim acquires the concurrent-extraction lock row. New, failed, and no-text
// rows are claimable; existing pending and done rows are not. Returns whether
// this caller won the claim.
func (r *Quotes) Claim(ctx context.Context, articleID int64) (*domain.ArticleQuotes, bool, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return nil, false, fmt.Errorf("begin claim tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	row, err := r.claimRow(ctx, tx, articleID)
	if errors.Is(err, pgx.ErrNoRows) {
		tag, err := tx.Exec(ctx, `
			INSERT INTO extraction_articlequotes
				(article_id, quotes, tldr, status, model, error, created_at, updated_at)
			VALUES ($1, '[]'::jsonb, '', 'pending', '', '', $2, $2)
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
	stalePending := row.Status == domain.QuotesStatusPending && row.UpdatedAt.Before(time.Now().Add(-15*time.Minute))
	if row.Status == domain.QuotesStatusFailed || row.Status == domain.QuotesStatusNoText || stalePending {
		now := time.Now()
		if _, err := tx.Exec(ctx, `
			UPDATE extraction_articlequotes SET
				status = 'pending', error = '', extracted_at = NULL, updated_at = $2
			WHERE article_id = $1`, articleID, now); err != nil {
			return nil, false, fmt.Errorf("reset quotes claim: %w", err)
		}
		row.Status = domain.QuotesStatusPending
		row.Error = ""
		row.ExtractedAt = nil
		row.UpdatedAt = now
		if err := tx.Commit(ctx); err != nil {
			return nil, false, fmt.Errorf("commit claim tx: %w", err)
		}
		return row, true, nil
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
		INSERT INTO extraction_articlequotes
			(article_id, quotes, tldr, status, extracted_at, model, error, created_at, updated_at)
		VALUES ($1, $2, $3, 'done', $4, $5, '', $6, $6)
		ON CONFLICT (article_id) DO UPDATE SET
			quotes = EXCLUDED.quotes, tldr = EXCLUDED.tldr, status = 'done',
			extracted_at = EXCLUDED.extracted_at, model = EXCLUDED.model,
			error = '', updated_at = EXCLUDED.updated_at`,
		articleID, payload, tldr, now, model, now)
	if err != nil {
		return fmt.Errorf("set done: %w", err)
	}
	return nil
}

// SetDoneFrozen atomically stamps the frozen markdown path and completes the
// extraction cache row after the file has been written.
func (r *Quotes) SetDoneFrozen(ctx context.Context, articleID int64, localPath string, quotes []domain.Quote, tldr, model string) error {
	payload, err := json.Marshal(quotes)
	if err != nil {
		return fmt.Errorf("marshal quotes: %w", err)
	}
	if payload == nil {
		payload = []byte("[]")
	}
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin frozen quotes tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	now := time.Now()
	if _, err := tx.Exec(ctx, `
		UPDATE articles_article SET local_md_path = $2, updated_at = $3 WHERE id = $1`,
		articleID, localPath, now); err != nil {
		return fmt.Errorf("stamp local markdown path: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO extraction_articlequotes
			(article_id, quotes, tldr, status, extracted_at, model, error, created_at, updated_at)
		VALUES ($1, $2, $3, 'done', $4, $5, '', $4, $4)
		ON CONFLICT (article_id) DO UPDATE SET
			quotes = EXCLUDED.quotes, tldr = EXCLUDED.tldr, status = 'done',
			extracted_at = EXCLUDED.extracted_at, model = EXCLUDED.model,
			error = '', updated_at = EXCLUDED.updated_at`,
		articleID, payload, tldr, now, model); err != nil {
		return fmt.Errorf("set frozen quotes done: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit frozen quotes tx: %w", err)
	}
	return nil
}

// SetNoText persists the "no extractable text" outcome.
func (r *Quotes) SetNoText(ctx context.Context, articleID int64, model string) error {
	now := time.Now()
	return r.setOutcome(ctx, articleID, domain.QuotesStatusNoText, model, "", &now)
}

// SetFailed persists a failed extraction with an error message.
func (r *Quotes) SetFailed(ctx context.Context, articleID int64, model, errMsg string) error {
	runes := []rune(errMsg)
	if len(runes) > 500 {
		errMsg = string(runes[:500])
	}
	return r.setOutcome(ctx, articleID, domain.QuotesStatusFailed, model, errMsg, nil)
}

func (r *Quotes) setOutcome(
	ctx context.Context,
	articleID int64,
	status, model, errMsg string,
	extractedAt *time.Time,
) error {
	now := time.Now()
	_, err := r.pool.Exec(ctx, `
		INSERT INTO extraction_articlequotes
			(article_id, quotes, tldr, status, model, error, extracted_at, created_at, updated_at)
		VALUES ($1, '[]'::jsonb, '', $2, $3, $4, $5, $6, $6)
		ON CONFLICT (article_id) DO UPDATE SET
			status = EXCLUDED.status, model = EXCLUDED.model, error = EXCLUDED.error,
			extracted_at = EXCLUDED.extracted_at, updated_at = EXCLUDED.updated_at`,
		articleID, status, model, errMsg, extractedAt, now)
	if err != nil {
		return fmt.Errorf("set outcome %s: %w", status, err)
	}
	return nil
}
