package service

import (
	"net/http"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"

	"github.com/4q4r/cindex/backend/internal/domain"
)

func TestParseCrossref(t *testing.T) {
	year := 2021
	data := map[string]any{
		"author": []any{
			map[string]any{"given": "Alice", "family": "Smith"},
			map[string]any{"given": "", "family": "Jones"},
			map[string]any{},
		},
		"published-print": map[string]any{
			"date-parts": []any{[]any{float64(2021), float64(3)}},
		},
		"volume": "12",
		"issue":  "4",
		"page":   "100-110",
	}
	e := parseCrossref(data)
	if len(e.authors) != 2 || e.authors[0] != "Alice Smith" || e.authors[1] != "Jones" {
		t.Errorf("authors = %v", e.authors)
	}
	if e.year == nil || *e.year != 2021 {
		t.Errorf("year = %v, want %d", e.year, year)
	}
	if e.volume != "12" || e.issue != "4" || e.pages != "100-110" {
		t.Errorf("volume/issue/pages = %q/%q/%q", e.volume, e.issue, e.pages)
	}
}

func TestParseCrossrefOnlineDateFallback(t *testing.T) {
	data := map[string]any{
		"published-online": map[string]any{
			"date-parts": []any{[]any{float64(2019)}},
		},
	}
	e := parseCrossref(data)
	if e.year == nil || *e.year != 2019 {
		t.Errorf("year = %v, want 2019", e.year)
	}
}

func TestParseOpenAlexInvertedIndex(t *testing.T) {
	data := map[string]any{
		"authorships": []any{
			map[string]any{"author": map[string]any{"display_name": "Carol White"}},
		},
		"abstract_inverted_index": map[string]any{
			"neural":   []any{float64(2)},
			"deep":     []any{float64(0)},
			"networks": []any{float64(3)},
			"learning": []any{float64(1)},
		},
		"publication_year": float64(2020),
		"biblio": map[string]any{
			"volume": "5", "issue": "2", "first_page": "10", "last_page": "20",
		},
	}
	e := parseOpenAlex(data)
	if len(e.authors) != 1 || e.authors[0] != "Carol White" {
		t.Errorf("authors = %v", e.authors)
	}
	if e.abstract != "deep learning neural networks" {
		t.Errorf("abstract = %q", e.abstract)
	}
	if e.year == nil || *e.year != 2020 {
		t.Errorf("year = %v", e.year)
	}
	if e.volume != "5" || e.issue != "2" || e.pages != "10-20" {
		t.Errorf("biblio = %q/%q/%q", e.volume, e.issue, e.pages)
	}
}

func TestParseSemanticScholar(t *testing.T) {
	data := map[string]any{
		"authors": []any{
			map[string]any{"name": "Dan Kim"},
		},
		"abstract": "  padded abstract  ",
		"year":     float64(2018),
	}
	e := parseSemanticScholar(data)
	if len(e.authors) != 1 || e.authors[0] != "Dan Kim" {
		t.Errorf("authors = %v", e.authors)
	}
	if e.abstract != "padded abstract" {
		t.Errorf("abstract = %q", e.abstract)
	}
	if e.year == nil || *e.year != 2018 {
		t.Errorf("year = %v", e.year)
	}
}

func TestApplyCascadeOrderAndTruncation(t *testing.T) {
	a := &domain.Article{Volume: "1", Issue: "1", Pages: "1-2"} // only authors/abstract/year missing
	missing := map[string]bool{"authors": true, "abstract": true, "year": true}
	yearA, yearB := 2000, 2001
	enrichments := []enrichment{
		{authors: []string{"First Source"}, abstract: strings.Repeat("a", 9000), year: &yearA},
		{authors: []string{"Second Source"}, abstract: "second abstract", year: &yearB},
	}
	changed, pending := applyCascade(a, missing, enrichments)
	if !changed {
		t.Fatal("applyCascade must report changed")
	}
	if len(pending) != 1 || pending[0] != "First Source" {
		t.Errorf("pending authors = %v, want first source only", pending)
	}
	if len(a.Abstract) != 8000 {
		t.Errorf("abstract truncated to %d, want 8000", len(a.Abstract))
	}
	if a.PubYear == nil || *a.PubYear != 2000 {
		t.Errorf("year = %v, want first source 2000", a.PubYear)
	}
	if a.Volume != "1" || a.Issue != "1" || a.Pages != "1-2" {
		t.Errorf("prefilled fields must not be touched: %+v", a)
	}
}

func TestApplyCascadeSecondStepFillsRemaining(t *testing.T) {
	a := &domain.Article{}
	missing := map[string]bool{"authors": true, "year": true}
	year := 1999
	enrichments := []enrichment{
		{year: &year}, // crossref without authors
		{authors: []string{"Late"}, year: &year},
	}
	changed, pending := applyCascade(a, missing, enrichments)
	if !changed || len(pending) != 1 || pending[0] != "Late" {
		t.Errorf("changed=%v pending=%v", changed, pending)
	}
}

func TestApplyCascadeTruncatesAbstractByRune(t *testing.T) {
	a := &domain.Article{}
	changed, _ := applyCascade(a, map[string]bool{"abstract": true}, []enrichment{{
		abstract: strings.Repeat("界", 9000),
	}})
	if !changed || utf8.RuneCountInString(a.Abstract) != 8000 || !utf8.ValidString(a.Abstract) {
		t.Errorf("abstract rune count=%d valid=%v", utf8.RuneCountInString(a.Abstract), utf8.ValidString(a.Abstract))
	}
}

func TestMissingFields(t *testing.T) {
	year := 2020
	a := &domain.Article{Abstract: "x", PubYear: &year, Volume: "1", Issue: "2", Pages: "3-4"}
	if m := missingFields(EnrichCandidate{Article: a, Authors: []string{"Bob"}}); len(m) != 0 {
		t.Errorf("fully populated article reported missing: %v", m)
	}
	m := missingFields(EnrichCandidate{Article: a, Authors: []string{"Unknown author"}})
	if !m["authors"] {
		t.Error("Unknown author must count as missing authors")
	}
	empty := &domain.Article{}
	m = missingFields(EnrichCandidate{Article: empty})
	for _, f := range []string{"authors", "abstract", "year", "volume", "issue", "pages"} {
		if !m[f] {
			t.Errorf("empty article must report %q missing", f)
		}
	}
	noDoi := &domain.Article{DOI: "not-a-doi"}
	_ = noDoi
}

func TestDoiEnrichmentSharedDependenciesAreConcurrentSafe(t *testing.T) {
	d := &DoiEnrichmentService{}
	clients := make(chan *http.Client, 32)
	limiters := make(chan *limiter, 32)
	var wg sync.WaitGroup
	for range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			clients <- d.httpClient()
			limiters <- d.rateLimiter()
		}()
	}
	wg.Wait()
	close(clients)
	close(limiters)
	var firstClient *http.Client
	for client := range clients {
		if firstClient == nil {
			firstClient = client
		} else if client != firstClient {
			t.Fatal("httpClient returned multiple instances")
		}
	}
	var firstLimiter *limiter
	for rateLimiter := range limiters {
		if firstLimiter == nil {
			firstLimiter = rateLimiter
		} else if rateLimiter != firstLimiter {
			t.Fatal("rateLimiter returned multiple instances")
		}
	}
}
