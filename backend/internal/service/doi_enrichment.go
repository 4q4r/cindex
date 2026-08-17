package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/4q4r/cindex/backend/internal/domain"
	"github.com/4q4r/cindex/backend/internal/repository"
)

// Rate-limit intervals (parity with _CROSSREF_INTERVAL etc. in
// apps.ingestion.doi_enrichment).
const (
	crossrefInterval = 100 * time.Millisecond
	openalexInterval = 10 * time.Millisecond
	s2Interval       = 1 * time.Second
)

// DoiEnrichmentService backfills missing article metadata from Crossref,
// OpenAlex and Semantic Scholar (parity with DoiEnrichmentService in
// apps.ingestion.doi_enrichment).
type DoiEnrichmentService struct {
	Articles    *repository.Articles
	Mailto      string
	OpenAlexKey string
	Logger      *slog.Logger
	client      *http.Client
	clientOnce  sync.Once
	limiter     *limiter
	limiterOnce sync.Once
}

// EnrichCandidate pairs a saved article with the author names recorded at
// save time (author state lives in the join tables, not the article row).
type EnrichCandidate struct {
	Article *domain.Article
	Authors []string
}

type enrichmentPair struct {
	cand    EnrichCandidate
	missing map[string]bool
}

// enrichment is one API's parsed metadata payload.
type enrichment struct {
	authors  []string
	abstract string
	year     *int
	volume   string
	issue    string
	pages    string
}

// Enrich backfills missing fields for the candidates. Returns the number of
// articles whose metadata changed.
func (d *DoiEnrichmentService) Enrich(ctx context.Context, candidates []EnrichCandidate) int {
	var withMissing []enrichmentPair
	for _, c := range candidates {
		if c.Article == nil || c.Article.DOI == "" || !strings.HasPrefix(c.Article.DOI, "10.") {
			continue
		}
		m := missingFields(c)
		if len(m) == 0 {
			continue
		}
		withMissing = append(withMissing, enrichmentPair{cand: c, missing: m})
	}
	if len(withMissing) == 0 {
		return 0
	}

	// Phase 1: parallel HTTP fetches only — no DB access.
	fetched := d.fetchAll(ctx, withMissing)

	// Phase 2: sync DB writes.
	updated := 0
	for _, p := range withMissing {
		enrichments, ok := fetched[p.cand.Article.ID]
		if !ok || len(enrichments) == 0 {
			continue
		}
		changed, pendingAuthors := applyCascade(p.cand.Article, p.missing, enrichments)
		if !changed {
			continue
		}
		if err := d.Articles.UpdateEnriched(ctx, p.cand.Article.ID,
			p.cand.Article.PubYear, p.cand.Article.Abstract,
			p.cand.Article.Volume, p.cand.Article.Issue, p.cand.Article.Pages,
			pendingAuthors); err != nil {
			d.logger().Error("doi_enrichment: save failed", "doi", p.cand.Article.DOI, "error", err)
			continue
		}
		updated++
	}
	return updated
}

// missingFields returns the set of empty field names (parity with
// _missing_fields: "Unknown author" counts as missing authors).
func missingFields(c EnrichCandidate) map[string]bool {
	out := map[string]bool{}
	hasUnknown := len(c.Authors) == 0
	for _, n := range c.Authors {
		if strings.TrimSpace(n) == "Unknown author" {
			hasUnknown = true
		}
	}
	if hasUnknown {
		out["authors"] = true
	}
	if c.Article.Abstract == "" {
		out["abstract"] = true
	}
	if c.Article.PubYear == nil {
		out["year"] = true
	}
	if c.Article.Volume == "" {
		out["volume"] = true
	}
	if c.Article.Issue == "" {
		out["issue"] = true
	}
	if c.Article.Pages == "" {
		out["pages"] = true
	}
	return out
}

type limiter struct {
	rates       map[string]*apiRate
	minInterval map[string]time.Duration
}

type apiRate struct {
	mu   sync.Mutex
	last time.Time
}

func newLimiter() *limiter {
	return &limiter{
		rates: map[string]*apiRate{
			"crossref": {}, "openalex": {}, "s2": {},
		},
		minInterval: map[string]time.Duration{
			"crossref": crossrefInterval,
			"openalex": openalexInterval,
			"s2":       s2Interval,
		},
	}
}

// wait sleeps until the minimum interval for api has elapsed.
func (l *limiter) wait(ctx context.Context, api string) error {
	rate := l.rates[api]
	rate.mu.Lock()
	defer rate.mu.Unlock()
	elapsed := time.Since(rate.last)
	if minI := l.minInterval[api]; elapsed < minI {
		timer := time.NewTimer(minI - elapsed)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	rate.last = time.Now()
	return nil
}

// fetchAll fetches enrichment data for every candidate in parallel (parity
// with _fetch_enrichments: Crossref → OpenAlex → Semantic Scholar cascade).
func (d *DoiEnrichmentService) fetchAll(ctx context.Context, pairs []enrichmentPair) map[int64][]enrichment {
	lim := d.rateLimiter()
	out := make(map[int64][]enrichment)
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, p := range pairs {
		wg.Add(1)
		go func(c EnrichCandidate, missing map[string]bool) {
			defer wg.Done()
			enrichments := d.fetchOne(ctx, c, missing, lim)
			if len(enrichments) == 0 {
				return
			}
			mu.Lock()
			out[c.Article.ID] = enrichments
			mu.Unlock()
		}(p.cand, p.missing)
	}
	wg.Wait()
	return out
}

func (d *DoiEnrichmentService) fetchOne(ctx context.Context, c EnrichCandidate, missing map[string]bool, lim *limiter) []enrichment {
	doi := c.Article.DOI
	var out []enrichment

	crossrefFields := map[string]bool{"authors": true, "year": true, "volume": true, "issue": true, "pages": true}
	if intersect(missing, crossrefFields) {
		if data := d.fetchCrossref(ctx, doi, lim); data != nil {
			out = append(out, parseCrossref(data))
		}
	}

	openalexFields := map[string]bool{"authors": true, "abstract": true, "year": true, "volume": true, "issue": true, "pages": true}
	if intersect(missing, openalexFields) {
		if data := d.fetchOpenAlex(ctx, doi, lim); data != nil {
			out = append(out, parseOpenAlex(data))
		}
	}

	s2Fields := map[string]bool{"authors": true, "abstract": true, "year": true}
	if intersect(missing, s2Fields) {
		if data := d.fetchSemanticScholar(ctx, doi, lim); data != nil {
			out = append(out, parseSemanticScholar(data))
		}
	}
	return out
}

func intersect(missing, want map[string]bool) bool {
	for k := range want {
		if missing[k] {
			return true
		}
	}
	return false
}

func (d *DoiEnrichmentService) getJSON(ctx context.Context, u string, lim *limiter, api string, out any) error {
	if err := lim.wait(ctx, api); err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", d.MailtoUserAgent())
	resp, err := d.httpClient().Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode == http.StatusNotFound {
		return errNotFound
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("doi_enrichment: %s status %d", api, resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(out)
}

var errNotFound = errors.New("not found")

// MailtoUserAgent builds the polite-pool mailto, defaulting to the Django
// fallback.
func (d *DoiEnrichmentService) MailtoUserAgent() string {
	if d.Mailto == "" {
		return "cindex@app.local"
	}
	return d.Mailto
}

func (d *DoiEnrichmentService) httpClient() *http.Client {
	d.clientOnce.Do(func() {
		if d.client == nil {
			d.client = &http.Client{Timeout: 30 * time.Second}
		}
	})
	return d.client
}

func (d *DoiEnrichmentService) rateLimiter() *limiter {
	d.limiterOnce.Do(func() { d.limiter = newLimiter() })
	return d.limiter
}

func (d *DoiEnrichmentService) logger() *slog.Logger {
	if d.Logger != nil {
		return d.Logger
	}
	return slog.Default()
}

func (d *DoiEnrichmentService) fetchCrossref(ctx context.Context, doi string, lim *limiter) map[string]any {
	u := "https://api.crossref.org/works/" + url.PathEscape(doi) + "?mailto=" + url.QueryEscape(d.MailtoUserAgent())
	var payload struct {
		Message map[string]any `json:"message"`
	}
	if err := d.getJSON(ctx, u, lim, "crossref", &payload); err != nil {
		d.logger().Debug("crossref: DOI lookup failed", "doi", doi, "error", err)
		return nil
	}
	return payload.Message
}

func (d *DoiEnrichmentService) fetchOpenAlex(ctx context.Context, doi string, lim *limiter) map[string]any {
	u := "https://api.openalex.org/works/doi:" + url.PathEscape(doi)
	if d.OpenAlexKey != "" {
		u += "?api_key=" + url.QueryEscape(d.OpenAlexKey)
	}
	var payload map[string]any
	if err := d.getJSON(ctx, u, lim, "openalex", &payload); err != nil {
		d.logger().Debug("openalex: DOI lookup failed", "doi", doi, "error", err)
		return nil
	}
	return payload
}

func (d *DoiEnrichmentService) fetchSemanticScholar(ctx context.Context, doi string, lim *limiter) map[string]any {
	u := "https://api.semanticscholar.org/graph/v1/paper/DOI:" + url.PathEscape(doi) +
		"?fields=title,authors,year,abstract,venue,journal"
	var payload map[string]any
	if err := d.getJSON(ctx, u, lim, "s2", &payload); err != nil {
		d.logger().Debug("s2: DOI lookup failed", "doi", doi, "error", err)
		return nil
	}
	return payload
}

// parseCrossref mirrors _parse_crossref.
func parseCrossref(data map[string]any) enrichment {
	var out enrichment
	if authors, ok := data["author"].([]any); ok {
		for _, a := range authors {
			if m, ok2 := a.(map[string]any); ok2 {
				given, _ := m["given"].(string)
				family, _ := m["family"].(string)
				name := strings.TrimSpace(given + " " + family)
				if name != "" {
					out.authors = append(out.authors, name)
				}
			}
		}
	}
	published := data["published-print"]
	if published == nil {
		published = data["published-online"]
	}
	if pm, ok := published.(map[string]any); ok {
		if parts, ok2 := pm["date-parts"].([]any); ok2 && len(parts) > 0 {
			if first, ok3 := parts[0].([]any); ok3 && len(first) > 0 {
				if y, ok4 := first[0].(float64); ok4 {
					year := int(y)
					out.year = &year
				}
			}
		}
	}
	out.volume = strOf(data["volume"])
	out.issue = strOf(data["issue"])
	out.pages = strOf(data["page"])
	return out
}

// parseOpenAlex mirrors _parse_openalex (including the inverted-index
// abstract reconstruction).
func parseOpenAlex(data map[string]any) enrichment {
	var out enrichment
	if authorships, ok := data["authorships"].([]any); ok {
		for _, a := range authorships {
			if m, ok2 := a.(map[string]any); ok2 {
				if author, ok3 := m["author"].(map[string]any); ok3 {
					if name, ok4 := author["display_name"].(string); ok4 && name != "" {
						out.authors = append(out.authors, name)
					}
				}
			}
		}
	}
	if idx, ok := data["abstract_inverted_index"].(map[string]any); ok {
		out.abstract = reconstructAbstract(idx)
	}
	if y, ok := data["publication_year"].(float64); ok {
		year := int(y)
		out.year = &year
	}
	if biblio, ok := data["biblio"].(map[string]any); ok {
		out.volume = strOf(biblio["volume"])
		out.issue = strOf(biblio["issue"])
		first := strOf(biblio["first_page"])
		last := strOf(biblio["last_page"])
		if first != "" {
			if last != "" {
				out.pages = first + "-" + last
			} else {
				out.pages = first
			}
		}
	}
	return out
}

// reconstructAbstract rebuilds plain text from the OpenAlex inverted index.
func reconstructAbstract(index map[string]any) string {
	type wordPos struct {
		pos  int
		word string
	}
	var positions []wordPos
	for word, raw := range index {
		if list, ok := raw.([]any); ok {
			for _, p := range list {
				if n, ok2 := p.(float64); ok2 {
					positions = append(positions, wordPos{pos: int(n), word: word})
				}
			}
		}
	}
	sort.Slice(positions, func(i, j int) bool { return positions[i].pos < positions[j].pos })
	parts := make([]string, 0, len(positions))
	for _, p := range positions {
		parts = append(parts, p.word)
	}
	return strings.Join(parts, " ")
}

// parseSemanticScholar mirrors _parse_semantic_scholar.
func parseSemanticScholar(data map[string]any) enrichment {
	var out enrichment
	if authors, ok := data["authors"].([]any); ok {
		for _, a := range authors {
			if m, ok2 := a.(map[string]any); ok2 {
				if name, ok3 := m["name"].(string); ok3 && name != "" {
					out.authors = append(out.authors, name)
				}
			}
		}
	}
	if abstract, ok := data["abstract"].(string); ok && strings.TrimSpace(abstract) != "" {
		out.abstract = strings.TrimSpace(abstract)
	}
	if y, ok := data["year"].(float64); ok {
		year := int(y)
		out.year = &year
	}
	return out
}

// applyCascade applies enrichment dicts in cascade order, filling only
// missing fields and truncating the abstract at 8000 chars (parity with
// _apply_cascade + _apply_step). Returns changed + the pending author names.
func applyCascade(article *domain.Article, initialMissing map[string]bool, enrichments []enrichment) (bool, []string) {
	missing := make(map[string]bool, len(initialMissing))
	for k, v := range initialMissing {
		missing[k] = v
	}
	changed := false
	var pendingAuthors []string
	for _, e := range enrichments {
		if len(e.authors) > 0 && missing["authors"] && len(pendingAuthors) == 0 {
			pendingAuthors = e.authors
			changed = true
		}
		if e.abstract != "" && missing["abstract"] {
			e.abstract = truncateRunes(e.abstract, 8000)
			article.Abstract = e.abstract
			changed = true
		}
		if e.year != nil && missing["year"] {
			article.PubYear = e.year
			changed = true
		}
		if e.volume != "" && missing["volume"] {
			article.Volume = e.volume
			changed = true
		}
		if e.issue != "" && missing["issue"] {
			article.Issue = e.issue
			changed = true
		}
		if e.pages != "" && missing["pages"] {
			article.Pages = e.pages
			changed = true
		}
		for _, f := range []string{"year", "volume", "issue", "pages", "abstract"} {
			if fieldFilled(article, f) {
				delete(missing, f)
			}
		}
		if len(pendingAuthors) > 0 {
			delete(missing, "authors")
		}
	}
	return changed, pendingAuthors
}

func fieldFilled(a *domain.Article, field string) bool {
	switch field {
	case "year":
		return a.PubYear != nil
	case "volume":
		return a.Volume != ""
	case "issue":
		return a.Issue != ""
	case "pages":
		return a.Pages != ""
	case "abstract":
		return a.Abstract != ""
	}
	return false
}

func strOf(v any) string {
	switch t := v.(type) {
	case string:
		return strings.TrimSpace(t)
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	}
	return ""
}

func truncateRunes(value string, max int) string {
	runes := []rune(value)
	if len(runes) <= max {
		return value
	}
	return string(runes[:max])
}
