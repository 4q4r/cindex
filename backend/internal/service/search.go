package service

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
)

// DefaultTopK mirrors APP.search_final_top_k.
const DefaultTopK = 30

// Search orchestrates the article-level search pipeline (parity with
// apps.search.services.SearchService).
type Search struct {
	articles  *repository.Articles
	quotes    *repository.Quotes
	translate *Translate
	topK      int
}

// NewSearch builds the search service.
func NewSearch(articles *repository.Articles, quotes *repository.Quotes, translate *Translate) *Search {
	return &Search{
		articles:  articles,
		quotes:    quotes,
		translate: translate,
		topK:      DefaultTopK,
	}
}

// SetTopK overrides the final top-K result cap.
func (s *Search) SetTopK(topK int) {
	if topK > 0 {
		s.topK = topK
	}
}

// SearchText combines query and expression into one normalized string
// (parity with SearchService._search_text).
func (s *Search) SearchText(query, expression string) string {
	var parts []string
	for _, p := range []string{query, expression} {
		if strings.TrimSpace(p) != "" {
			parts = append(parts, strings.TrimSpace(p))
		}
	}
	return NormalizeScholarlyText(strings.Join(parts, " "), 512)
}

// SearchTerms returns the casefolded word terms of the combined text
// (parity with SearchService._search_terms).
func (s *Search) SearchTerms(query, expression string) []string {
	text := strings.ToLower(s.SearchText(query, expression))
	if text == "" {
		return nil
	}
	terms := make([]string, 0)
	for _, t := range strings.Fields(text) {
		if t != "" {
			terms = append(terms, t)
		}
	}
	if len(terms) == 0 {
		terms = []string{text}
	}
	return terms
}

// CrossLingualTerms returns the translated phrases and their tokens, built
// from the public query (parity with SearchService._cross_lingual_terms).
func (s *Search) CrossLingualTerms(ctx context.Context, query string) ([]string, []string) {
	phrases := s.translate.ExpandSearchTerms(ctx, query)
	var tokens []string
	for _, translated := range phrases {
		for _, t := range strings.Fields(translated) {
			if len(t) > 1 {
				tokens = append(tokens, t)
			}
		}
	}
	return phrases, tokens
}

// IndexHitCount returns the unfiltered corpus hit count (parity with
// SearchService.index_hit_count).
func (s *Search) IndexHitCount(ctx context.Context, query, expression string) (int, error) {
	searchText := s.SearchText(query, expression)
	if searchText == "" {
		return 0, nil
	}
	terms := s.SearchTerms(query, expression)
	crossPhrases, _ := s.CrossLingualTerms(ctx, query)
	q := repository.SearchQuery{
		SearchText:  searchText,
		Terms:       terms,
		CrossTokens: nil,
		FTSPhrases:  append([]string{searchText}, crossPhrases...),
		Filters:     domain.SearchFilters{},
		TopK:        1,
	}
	_, hitCount, err := s.articles.Search(ctx, q)
	if err != nil {
		return 0, err
	}
	return hitCount, nil
}

// Run executes the full pipeline and returns ranked payloads (parity with
// SearchService._run_index_search + run).
func (s *Search) Run(ctx context.Context, query, expression string, filters domain.SearchFilters) ([]domain.SearchHit, int, error) {
	searchText := s.SearchText(query, expression)
	if searchText == "" {
		return nil, 0, nil
	}
	terms := s.SearchTerms(query, expression)
	crossPhrases, crossTokens := s.CrossLingualTerms(ctx, query)
	ftsPhrases := append([]string{searchText}, crossPhrases...)

	q := repository.SearchQuery{
		SearchText:  searchText,
		Terms:       terms,
		CrossTokens: crossTokens,
		FTSPhrases:  ftsPhrases,
		Filters:     filters,
		TopK:        s.topK,
	}
	rows, hitCount, err := s.articles.Search(ctx, q)
	if err != nil {
		return nil, 0, fmt.Errorf("run index search: %w", err)
	}

	ids := make([]int64, 0, len(rows))
	for _, row := range rows {
		ids = append(ids, row.ID)
	}
	identifiers, err := s.articles.GetIdentifiers(ctx, ids)
	if err != nil {
		return nil, 0, fmt.Errorf("load identifiers: %w", err)
	}

	seen := make(map[string]bool, len(rows))
	var hits []domain.SearchHit
	for _, row := range rows {
		key := s.dedupeKey(&row)
		if seen[key] {
			continue
		}
		seen[key] = true
		hit := s.payload(&row, identifiers[row.ID])
		hits = append(hits, hit)
	}
	return hits, hitCount, nil
}

// RunWithQuotes is Run plus cached PERELMAN quotes/TLDR; direct requests never
// trigger LLM extraction.
func (s *Search) RunWithQuotes(ctx context.Context, query, expression string, filters domain.SearchFilters) ([]domain.SearchHit, int, error) {
	hits, hitCount, err := s.Run(ctx, query, expression, filters)
	if err != nil {
		return nil, 0, err
	}
	for i := range hits {
		cache, err := s.quotes.GetByArticle(ctx, hits[i].ID)
		if err != nil {
			continue
		}
		if cache.Status != domain.QuotesStatusDone {
			continue
		}
		hits[i].Quotes = cache.Quotes
		hits[i].TLDR = cache.TLDR
	}
	return hits, hitCount, nil
}

func (s *Search) dedupeKey(row *repository.SearchRow) string {
	if doi := NormalizeDOI(row.DOI); doi != "" {
		return "doi:" + doi
	}
	titleKey := CanonicalTextKey(row.Title)
	journalKey := CanonicalTextKey(row.Journal)
	year := ""
	if row.Year != nil {
		year = strconv.Itoa(*row.Year)
	}
	return strings.Join([]string{
		"title:" + titleKey,
		"year:" + year,
		"journal:" + journalKey,
	}, "|")
}

func (s *Search) payload(row *repository.SearchRow, identifiers []domain.Identifier) domain.SearchHit {
	preview := ""
	for _, candidate := range []string{row.Abstract, row.FullText, row.Title} {
		preview = NormalizeScholarlyText(candidate, 500)
		if preview != "" {
			break
		}
	}
	identMap := make(map[string]string, len(identifiers))
	for _, ident := range identifiers {
		identMap[ident.Kind] = ident.Value
	}
	return domain.SearchHit{
		ID:              row.ID,
		Title:           NormalizeScholarlyText(row.Title, 900),
		Preview:         NormalizeScholarlyText(preview, 500),
		Year:            row.Year,
		PublicationDate: row.PubDate,
		Source:          row.Source,
		Journal:         NormalizeScholarlyText(row.Journal, 300),
		Volume:          row.Volume,
		Issue:           row.Issue,
		Pages:           row.Pages,
		DOI:             row.DOI,
		Identifiers:     identMap,
		IsPeerReviewed:  row.IsPeerReviewed,
		Indexed:         row.Indexed,
		DOIAndCard:      row.DOIAndCard,
		NotPreprint:     row.NotPreprint,
		PeerReviewConf:  row.PeerReviewConf,
		IndexingConf:    row.IndexingConf,
		DOIAndCardConf:  row.DOIAndCardConf,
		NotPreprintConf: row.NotPreprintConf,
		OverallConf:     row.OverallConf,
		IsRetracted:     row.IsRetracted,
		RetractionNote:  row.RetractionNote,
		CitedByCount:    row.CitedByCount,
		Tier:            domain.TierLabel(row.IsPeerReviewed, row.PeerReviewConf),
		URL:             row.URL,
		RerankScore:     row.Score,
	}
}

// AttachAuthors loads and attaches ordered author names for the hits.
func (s *Search) AttachAuthors(ctx context.Context, hits []domain.SearchHit) error {
	for i := range hits {
		names, err := s.articles.GetAuthors(ctx, hits[i].ID)
		if err != nil {
			return fmt.Errorf("load authors for %d: %w", hits[i].ID, err)
		}
		hits[i].Authors = names
	}
	return nil
}
