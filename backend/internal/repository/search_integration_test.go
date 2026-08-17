package repository_test

import (
	"context"
	"testing"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
)

// seedSearchFixture inserts sources, journals and articles used by the search
// parity tests. Returns the article repo and the ids of the three "quantum"
// articles in descending score order.
func seedSearchFixture(t *testing.T) (*repository.Articles, []int64) {
	t.Helper()
	ctx := context.Background()
	pool := setupPool(t)

	src := repository.NewSources(pool)
	openalexID, err := src.EnsureExists(ctx, "openalex", "OpenAlex", "https://api.openalex.org")
	if err != nil {
		t.Fatal(err)
	}
	zenodoID, err := src.EnsureExists(ctx, "zenodo", "Zenodo", "https://zenodo.org")
	if err != nil {
		t.Fatal(err)
	}
	journalID, err := src.UpsertJournal(ctx, "Journal of Quantum Research")
	if err != nil {
		t.Fatal(err)
	}

	articles := repository.NewArticles(pool)
	year := func(y int) *int { return &y }

	mk := func(sourceID int64, journalID *int64, title, doi string, year *int, retracted bool) int64 {
		pubDate := time.Date(*year, 3, 1, 0, 0, 0, 0, time.UTC)
		a := &domain.Article{
			SourceID: sourceID, JournalID: journalID,
			Title: title, Abstract: "Modern approaches to scientific computing.",
			URL: "https://example.org/" + doi, DOI: doi,
			PubYear: year, PubDate: &pubDate,
			IsRetracted: retracted,
		}
		if doi == "10.1000/qa.0002" {
			a.Abstract = "An introduction to the fundamentals of quantum theory."
		}
		id, err := articles.Upsert(ctx, a)
		if err != nil {
			t.Fatal(err)
		}
		// Eligibility flags drive the peer_reviewed/indexed/preprint filters.
		got, err := articles.GetByID(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		got.PeerReviewEvidence = "tierA: openalex venue=journal"
		got.IndexingEvidence = "tierA: medline"
		u := domain.ApplyEligibility(got, "openalex", "Journal of Quantum Research")
		if err := articles.UpdateEligibility(ctx, id, u); err != nil {
			t.Fatal(err)
		}
		return id
	}

	// a1: full match, high score. a3: same title terms but zenodo penalty.
	a1 := mk(openalexID, &journalID, "Quantum computing advances", "10.1000/qa.0001", year(2023), false)
	mk(openalexID, &journalID, "Introduction to quantum mechanics", "10.1000/qa.0002", year(2021), false)
	a3 := mk(zenodoID, &journalID, "Quantum computing and AI", "10.1000/qa.0003", year(2024), false)
	a4 := mk(openalexID, &journalID, "Classical computers", "10.1000/qa.0004", year(2022), true)

	return articles, []int64{a1, a3, a4}
}

func TestSearchFTSRankingAndFilters(t *testing.T) {
	ctx := context.Background()
	articles, _ := seedSearchFixture(t)

	run := func(q repository.SearchQuery) ([]repository.SearchRow, int, error) {
		t.Helper()
		return articles.Search(ctx, q)
	}

	// Default relevance: both quantum articles match; full-text terms absent so
	// the score comes from title/abstract matches only.
	rows, hits, err := run(repository.SearchQuery{
		SearchText: "quantum computing",
		Terms:      []string{"quantum", "computing"},
		FTSPhrases: []string{"quantum computing"},
		TopK:       30,
	})
	if err != nil {
		t.Fatal(err)
	}
	if hits != 2 {
		t.Errorf("hit count = %d, want 2 (filters must not affect the count)", hits)
	}
	if len(rows) != 2 {
		t.Fatalf("rows = %d, want 2", len(rows))
	}
	if rows[0].DOI != "10.1000/qa.0001" {
		t.Errorf("top result = %s, want the non-zenodo article first", rows[0].DOI)
	}
	if rows[1].DOI != "10.1000/qa.0003" {
		t.Errorf("zenodo article must be ranked second: %s", rows[1].DOI)
	}

	// Filter: peer reviewed only.
	rows, _, err = run(repository.SearchQuery{
		SearchText: "quantum computing",
		Terms:      []string{"quantum", "computing"},
		FTSPhrases: []string{"quantum computing"},
		Filters:    domain.SearchFilters{PeerReviewedOnly: true},
		TopK:       30,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 {
		t.Errorf("peer_reviewed rows = %d, want 2", len(rows))
	}

	// Filter: year range slices to a single row.
	rows, _, err = run(repository.SearchQuery{
		SearchText: "quantum computing",
		Terms:      []string{"quantum", "computing"},
		FTSPhrases: []string{"quantum computing"},
		Filters:    domain.SearchFilters{YearFrom: intp(2024), YearTo: intp(2024)},
		TopK:       30,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].DOI != "10.1000/qa.0003" {
		t.Errorf("year filter rows = %+v, want only 2024 zenodo article", rows)
	}

	// Sort newest: 2024 first regardless of score.
	rows, _, err = run(repository.SearchQuery{
		SearchText: "quantum computing",
		Terms:      []string{"quantum", "computing"},
		FTSPhrases: []string{"quantum computing"},
		Filters:    domain.SearchFilters{SortBy: domain.SortNewest},
		TopK:       30,
	})
	if err != nil {
		t.Fatal(err)
	}
	if rows[0].DOI != "10.1000/qa.0003" {
		t.Errorf("newest sort top = %s, want 2024 article", rows[0].DOI)
	}

	// Top-K truncation.
	rows, _, err = run(repository.SearchQuery{
		SearchText: "quantum computing",
		Terms:      []string{"quantum", "computing"},
		FTSPhrases: []string{"quantum computing"},
		TopK:       1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Errorf("top-k rows = %d, want 1", len(rows))
	}

	// Exclude retracted: the retracted article never matched FTS anyway, but
	// the flag must not crash and must not change matched rows.
	rows, hits, err = run(repository.SearchQuery{
		SearchText: "quantum computing",
		Terms:      []string{"quantum", "computing"},
		FTSPhrases: []string{"quantum computing"},
		Filters:    domain.SearchFilters{ExcludeRetracted: true},
		TopK:       30,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || hits != 2 {
		t.Errorf("exclude_retracted rows=%d hits=%d", len(rows), hits)
	}
}

func intp(v int) *int { return &v }
