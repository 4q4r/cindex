// Package repository provides PostgreSQL persistence for the domain entities.
package repository

import (
	"context"
	"errors"
	"fmt"
	"strings"
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

// SearchQuery is a prepared parity query: the combined search text, its
// casefolded word terms, cross-lingual tokens (from translations only) and
// the phrases fed to websearch_to_tsquery (search text + translated phrases).
type SearchQuery struct {
	SearchText  string
	Terms       []string
	CrossTokens []string
	FTSPhrases  []string
	Filters     domain.SearchFilters
	TopK        int
}

// SearchRow is one scored search hit (scored in SQL, payload assembled in Go).
type SearchRow struct {
	ID             int64
	Title          string
	Abstract       string
	FullText       string
	DOI            string
	Source         string
	Journal        string
	Volume         string
	Issue          string
	Pages          string
	URL            string
	RetractionNote string
	Year           *int
	PubDate        *time.Time
	IsPeerReviewed bool
	Indexed        bool
	DOIAndCard     bool
	NotPreprint    bool
	IsRetracted    bool
	CitedByCount   int
	Score          float64
}

// Search runs the parity scoring query: FTS cross-lingual match, exact/term/
// cross-lingual Case scores with per-source penalty, filters applied before
// the top-K limit. Returns scored rows plus the unfiltered index hit count
// (parity with SearchService.search_articles).
func (r *Articles) Search(ctx context.Context, q SearchQuery) ([]SearchRow, int, error) {
	if strings.TrimSpace(q.SearchText) == "" || len(q.FTSPhrases) == 0 {
		return nil, 0, nil
	}
	if q.TopK <= 0 {
		q.TopK = 30
	}
	var args []any
	arg := func(v any) string {
		args = append(args, v)
		return fmt.Sprintf("$%d", len(args))
	}

	ftsClause := func(phrase string) string {
		return `to_tsvector('simple', coalesce(a.title, '') || ' ' ||
			coalesce(a.abstract, '') || ' ' || coalesce(a.full_text, '')) @@
			websearch_to_tsquery('simple', ` + arg(phrase) + `)`
	}
	ftsParts := make([]string, 0, len(q.FTSPhrases))
	for _, p := range q.FTSPhrases {
		if strings.TrimSpace(p) != "" {
			ftsParts = append(ftsParts, ftsClause(p))
		}
	}
	if len(ftsParts) == 0 {
		return nil, 0, nil
	}
	ftsWhere := "(" + strings.Join(ftsParts, " OR ") + ")"

	exact := arg(q.SearchText)
	exactCase := fmt.Sprintf(`CASE WHEN lower(a.doi) = lower(%s) THEN 10.0
		WHEN lower(a.title) = lower(%s) THEN 4.0
		WHEN a.title ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 2.0
		ELSE 0.0 END`, exact, exact, exact)

	termCases := make([]string, 0, len(q.Terms))
	for _, t := range q.Terms {
		p := arg(t)
		termCases = append(termCases, fmt.Sprintf(`CASE WHEN lower(a.doi) = lower(%s) THEN 10.0
			WHEN a.title ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 6.0
			WHEN a.abstract ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 4.0
			WHEN a.full_text ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 2.0
			WHEN COALESCE(j.name, '') ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 1.0
			ELSE 0.0 END`, p, p, p, p, p))
	}

	crossCases := make([]string, 0, len(q.CrossTokens))
	for _, tok := range q.CrossTokens {
		p := arg(tok)
		crossCases = append(crossCases, fmt.Sprintf(`CASE WHEN a.title ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 3.0
			WHEN a.abstract ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 2.0
			WHEN a.full_text ILIKE '%%' || %s || '%%' ESCAPE '\' THEN 1.0
			ELSE 0.0 END`, p, p, p))
	}

	scoreParts := []string{exactCase}
	if len(termCases) > 0 {
		scoreParts = append(scoreParts, strings.Join(termCases, " + "))
	}
	if len(crossCases) > 0 {
		scoreParts = append(scoreParts, strings.Join(crossCases, " + "))
	}
	scoreExpr := fmt.Sprintf("(%s) * CASE WHEN s.key = 'zenodo' THEN 0.3 ELSE 1.0 END",
		strings.Join(scoreParts, " + "))

	var where []string
	where = append(where, `a.doi LIKE '10.%'`, ftsWhere)
	if q.Filters.PeerReviewedOnly {
		where = append(where, "a.is_peer_reviewed_or_refereed = TRUE")
	}
	if q.Filters.IndexedOnly {
		where = append(where, "a.is_indexed_in_reputable_db = TRUE")
	}
	if q.Filters.ExcludePreprints {
		where = append(where, "a.is_not_preprint_or_author_manuscript = TRUE")
	}
	if q.Filters.ExcludeRetracted {
		where = append(where, "a.is_retracted = FALSE")
	}
	if q.Filters.YearFrom != nil {
		where = append(where, "a.publication_year >= "+arg(*q.Filters.YearFrom))
	}
	if q.Filters.YearTo != nil {
		where = append(where, "a.publication_year <= "+arg(*q.Filters.YearTo))
	}
	whereSQL := strings.Join(where, " AND ")

	countQuery := "SELECT COUNT(*) FROM articles_article a WHERE a.doi LIKE '10.%' AND " + ftsWhere
	var hitCount int
	// The count query only references the FTS placeholders ($1..$k), which are
	// the first arguments appended; later args belong to SELECT scoring and
	// filter clauses only.
	if err := r.pool.QueryRow(ctx, countQuery, args[:len(ftsParts)]...).Scan(&hitCount); err != nil {
		return nil, 0, fmt.Errorf("count index hits: %w", err)
	}

	order := "search_score DESC, a.publication_year DESC, a.updated_at DESC, a.id"
	selectExpr := scoreExpr
	switch q.Filters.NormalizedSort() {
	case domain.SortNewest:
		order = "a.publication_year DESC, a.updated_at DESC, a.id"
	case domain.SortMetadata:
		order = "metadata_score DESC, a.publication_year DESC, a.updated_at DESC, a.id"
		metadataScore := `(CASE WHEN a.is_peer_reviewed_or_refereed THEN 2 ELSE 0 END
			+ CASE WHEN a.is_indexed_in_reputable_db THEN 2 ELSE 0 END
			+ CASE WHEN a.has_doi_and_journal_card THEN 1 ELSE 0 END
			+ CASE WHEN a.is_not_preprint_or_author_manuscript THEN 1 ELSE 0 END
			+ CASE WHEN j.id IS NOT NULL THEN 1 ELSE 0 END)`
		selectExpr = metadataScore
	}

	query := fmt.Sprintf(`
		SELECT a.id, a.title, a.abstract, a.full_text, a.doi, s.name,
			COALESCE(j.name, ''), COALESCE(a.volume, ''), COALESCE(a.issue, ''),
			COALESCE(a.pages, ''), a.url, a.retraction_note,
			a.publication_year, a.publication_date,
			a.is_peer_reviewed_or_refereed, a.is_indexed_in_reputable_db,
			a.has_doi_and_journal_card, a.is_not_preprint_or_author_manuscript,
			a.is_retracted, a.cited_by_count, (%s) AS search_score
		FROM articles_article a
		JOIN articles_source s ON s.id = a.source_id
		LEFT JOIN articles_journal j ON j.id = a.journal_id
		WHERE %s
		ORDER BY %s
		LIMIT %s`, selectExpr, whereSQL, order, arg(q.TopK))

	rows, err := r.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, 0, fmt.Errorf("search articles: %w", err)
	}
	defer rows.Close()

	var hits []SearchRow
	for rows.Next() {
		var h SearchRow
		if err := rows.Scan(
			&h.ID, &h.Title, &h.Abstract, &h.FullText, &h.DOI, &h.Source,
			&h.Journal, &h.Volume, &h.Issue, &h.Pages, &h.URL, &h.RetractionNote,
			&h.Year, &h.PubDate, &h.IsPeerReviewed, &h.Indexed, &h.DOIAndCard,
			&h.NotPreprint, &h.IsRetracted, &h.CitedByCount, &h.Score,
		); err != nil {
			return nil, 0, fmt.Errorf("scan search row: %w", err)
		}
		hits = append(hits, h)
	}
	return hits, hitCount, rows.Err()
}

// GetIdentifiers returns all identifiers for the given article ids.
func (r *Articles) GetIdentifiers(ctx context.Context, ids []int64) (map[int64][]domain.Identifier, error) {
	if len(ids) == 0 {
		return map[int64][]domain.Identifier{}, nil
	}
	rows, err := r.pool.Query(ctx, `
		SELECT article_id, kind, value FROM articles_identifier
		WHERE article_id = ANY($1)`, ids)
	if err != nil {
		return nil, fmt.Errorf("query identifiers: %w", err)
	}
	defer rows.Close()

	out := make(map[int64][]domain.Identifier, len(ids))
	for rows.Next() {
		var id int64
		var ident domain.Identifier
		if err := rows.Scan(&id, &ident.Kind, &ident.Value); err != nil {
			return nil, fmt.Errorf("scan identifier: %w", err)
		}
		out[id] = append(out[id], ident)
	}
	return out, rows.Err()
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
