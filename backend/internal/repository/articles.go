// Package repository provides PostgreSQL persistence for the domain entities.
package repository

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ErrNotFound is returned when a queried row does not exist.
var ErrNotFound = errors.New("not found")

// Articles stores Article, Author, ArticleAuthor and Identifier rows.
type Articles struct {
	pool *pgxpool.Pool
}

// NewArticles builds the article repository over the given pool.
func NewArticles(pool *pgxpool.Pool) *Articles {
	return &Articles{pool: pool}
}

const articleCols = `
	a.id, a.source_id, a.journal_id, a.external_id, a.title, a.abstract,
	a.full_text, a.language, a.publication_year, a.publication_date, a.url, a.doi,
	a.local_md_path, a.volume, a.issue, a.pages, a.is_open_access, a.is_retracted,
	a.retraction_note, a.cited_by_count, a.is_peer_reviewed_or_refereed,
	a.is_indexed_in_reputable_db, a.has_doi_and_journal_card,
	a.is_not_preprint_or_author_manuscript, a.search_vector, a.is_eligible,
	a.peer_review_confidence, a.indexing_confidence, a.doi_and_card_confidence,
	a.not_preprint_confidence, a.eligibility_confidence, a.peer_review_evidence,
	a.indexing_evidence, a.doi_journal_evidence, a.preprint_evidence,
	a.created_at, a.updated_at`

func scanArticle(row pgx.Row) (*domain.Article, error) {
	var a domain.Article
	err := row.Scan(
		&a.ID, &a.SourceID, &a.JournalID, &a.ExternalID, &a.Title, &a.Abstract,
		&a.FullText, &a.Language, &a.PubYear, &a.PubDate, &a.URL, &a.DOI,
		&a.LocalMDPath, &a.Volume, &a.Issue, &a.Pages, &a.IsOpenAccess, &a.IsRetracted,
		&a.RetractionNote, &a.CitedByCount, &a.IsPeerReviewedOrRefereed,
		&a.IsIndexedInReputableDB, &a.HasDOIAndJournalCard,
		&a.IsNotPreprintOrManuscript, &a.SearchVector, &a.IsEligible,
		&a.PeerReviewConfidence, &a.IndexingConfidence, &a.DOIAndCardConfidence,
		&a.NotPreprintConfidence, &a.EligibilityConfidence, &a.PeerReviewEvidence,
		&a.IndexingEvidence, &a.DOIJournalEvidence, &a.PreprintEvidence,
		&a.CreatedAt, &a.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("scan article: %w", err)
	}
	return &a, nil
}

// GetByID loads an article (without journal/author joins).
func (r *Articles) GetByID(ctx context.Context, id int64) (*domain.Article, error) {
	return scanArticle(r.pool.QueryRow(ctx,
		`SELECT `+articleCols+` FROM articles_article a WHERE a.id = $1`, id))
}

// GetByDOI loads an article by its unique DOI.
func (r *Articles) GetByDOI(ctx context.Context, doi string) (*domain.Article, error) {
	return scanArticle(r.pool.QueryRow(ctx,
		`SELECT `+articleCols+` FROM articles_article a WHERE a.doi = $1`, doi))
}

// GetByURL loads an article by its URL (indexed, non-unique; newest wins).
func (r *Articles) GetByURL(ctx context.Context, url string) (*domain.Article, error) {
	return scanArticle(r.pool.QueryRow(ctx,
		`SELECT `+articleCols+` FROM articles_article a WHERE a.url = $1 ORDER BY a.id DESC LIMIT 1`, url))
}

// Upsert inserts an article keyed on its unique DOI, or updates the existing
// row in place; returns the row id. Parity with the Django get_or_create
// doi-keyed ingestion path.
func (r *Articles) Upsert(ctx context.Context, a *domain.Article) (int64, error) {
	var id int64
	err := r.pool.QueryRow(ctx, `
		INSERT INTO articles_article (
			source_id, journal_id, external_id, title, abstract, full_text,
			language, publication_year, publication_date, url, doi, local_md_path,
			volume, issue, pages, is_open_access, is_retracted, retraction_note,
			cited_by_count, is_peer_reviewed_or_refereed, is_indexed_in_reputable_db,
			has_doi_and_journal_card, is_not_preprint_or_author_manuscript,
			search_vector, is_eligible, peer_review_confidence, indexing_confidence,
			doi_and_card_confidence, not_preprint_confidence, eligibility_confidence,
			peer_review_evidence, indexing_evidence, doi_journal_evidence,
			preprint_evidence, created_at, updated_at
		) VALUES (
			$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
			$16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28,
			$29, $30, $31, $32, $33, $34, $35, $36
		)
		ON CONFLICT (doi) DO UPDATE SET
			source_id = EXCLUDED.source_id,
			journal_id = EXCLUDED.journal_id,
			external_id = EXCLUDED.external_id,
			title = EXCLUDED.title,
			abstract = EXCLUDED.abstract,
			full_text = EXCLUDED.full_text,
			language = EXCLUDED.language,
			publication_year = EXCLUDED.publication_year,
			publication_date = EXCLUDED.publication_date,
			url = EXCLUDED.url,
			volume = EXCLUDED.volume,
			issue = EXCLUDED.issue,
			pages = EXCLUDED.pages,
			is_open_access = EXCLUDED.is_open_access,
			is_retracted = EXCLUDED.is_retracted,
			retraction_note = EXCLUDED.retraction_note,
			cited_by_count = EXCLUDED.cited_by_count,
			updated_at = EXCLUDED.updated_at
		RETURNING id`,
		a.SourceID, nullableInt64(a.JournalID), a.ExternalID, a.Title, a.Abstract,
		a.FullText, a.Language, nullableInt(a.PubYear), nullableTime(a.PubDate),
		a.URL, a.DOI, a.LocalMDPath, a.Volume, a.Issue, a.Pages, a.IsOpenAccess,
		a.IsRetracted, a.RetractionNote, a.CitedByCount, a.IsPeerReviewedOrRefereed,
		a.IsIndexedInReputableDB, a.HasDOIAndJournalCard, a.IsNotPreprintOrManuscript,
		a.SearchVector, a.IsEligible, a.PeerReviewConfidence, a.IndexingConfidence,
		a.DOIAndCardConfidence, a.NotPreprintConfidence, a.EligibilityConfidence,
		a.PeerReviewEvidence, a.IndexingEvidence, a.DOIJournalEvidence,
		a.PreprintEvidence, time.Now(), time.Now(),
	).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("upsert article: %w", err)
	}
	return id, nil
}

// UpdateEligibility persists an EligibilityUpdate computed by the domain
// service. Parity with ArticleEligibilityService.apply.
func (r *Articles) UpdateEligibility(ctx context.Context, id int64, u domain.EligibilityUpdate) error {
	_, err := r.pool.Exec(ctx, `
		UPDATE articles_article SET
			is_peer_reviewed_or_refereed = $2,
			is_indexed_in_reputable_db = $3,
			has_doi_and_journal_card = $4,
			is_not_preprint_or_author_manuscript = $5,
			is_eligible = $6,
			peer_review_confidence = $7,
			indexing_confidence = $8,
			doi_and_card_confidence = $9,
			not_preprint_confidence = $10,
			eligibility_confidence = $11,
			peer_review_evidence = $12,
			preprint_evidence = $13,
			retraction_note = $14,
			updated_at = $15
		WHERE id = $1`,
		id,
		u.IsPeerReviewedOrRefereed, u.IsIndexedInReputableDB, u.HasDOIAndJournalCard,
		u.IsNotPreprintOrManuscript, u.IsEligible, u.PeerReviewConfidence,
		u.IndexingConfidence, u.DOIAndCardConfidence, u.NotPreprintConfidence,
		u.EligibilityConfidence, u.PeerReviewEvidence, u.PreprintEvidence,
		u.RetractionNote, time.Now(),
	)
	if err != nil {
		return fmt.Errorf("update eligibility: %w", err)
	}
	return nil
}

// GetAuthors returns the ordered author names for an article.
func (r *Articles) GetAuthors(ctx context.Context, articleID int64) ([]string, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT au.full_name FROM articles_articleauthor aa
		JOIN articles_author au ON au.id = aa.author_id
		WHERE aa.article_id = $1 ORDER BY aa."order"`, articleID)
	if err != nil {
		return nil, fmt.Errorf("query authors: %w", err)
	}
	defer rows.Close()

	var names []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, fmt.Errorf("scan author: %w", err)
		}
		names = append(names, n)
	}
	return names, rows.Err()
}

// ReplaceAuthors resets the author list of an article to the given names.
func (r *Articles) ReplaceAuthors(ctx context.Context, articleID int64, names []string) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin authors tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx,
		`DELETE FROM articles_articleauthor WHERE article_id = $1`, articleID); err != nil {
		return fmt.Errorf("delete article authors: %w", err)
	}
	for i, name := range names {
		var authorID int64
		if err := tx.QueryRow(ctx, `
			INSERT INTO articles_author (full_name) VALUES ($1)
			ON CONFLICT DO NOTHING
			RETURNING id`, name).Scan(&authorID); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				if err := tx.QueryRow(ctx,
					`SELECT id FROM articles_author WHERE full_name = $1`, name).Scan(&authorID); err != nil {
					return fmt.Errorf("find author: %w", err)
				}
			} else {
				return fmt.Errorf("insert author: %w", err)
			}
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO articles_articleauthor (article_id, author_id, "order")
			VALUES ($1, $2, $3)
			ON CONFLICT (article_id, author_id, "order") DO NOTHING`,
			articleID, authorID, i+1); err != nil {
			return fmt.Errorf("link author: %w", err)
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit authors tx: %w", err)
	}
	return nil
}

// UpsertIdentifier adds an identifier unless it already exists (get_or_create
// parity with IdentifierService.upsert).
func (r *Articles) UpsertIdentifier(ctx context.Context, articleID int64, kind, value string) error {
	if value == "" {
		return nil
	}
	_, err := r.pool.Exec(ctx, `
		INSERT INTO articles_identifier (article_id, kind, value)
		VALUES ($1, $2, $3)
		ON CONFLICT (article_id, kind, value) DO NOTHING`,
		articleID, kind, value)
	if err != nil {
		return fmt.Errorf("upsert identifier: %w", err)
	}
	return nil
}

func nullableInt64(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}

func nullableInt(p *int) any {
	if p == nil {
		return nil
	}
	return *p
}

func nullableTime(p *time.Time) any {
	if p == nil {
		return nil
	}
	return *p
}
