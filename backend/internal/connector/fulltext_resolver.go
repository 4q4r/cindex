package connector

import (
	"context"
	"net"
	"net/url"
	"strings"
	"unicode/utf8"
)

const (
	defaultUnpaywallBase = "https://api.unpaywall.org/v2"
	defaultEuropePMCBase = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
	maxOACandidates      = 12
	maxResolvedTextRunes = 2_000_000
)

// LawfulFullTextResolver augments existing text only from open-access URLs
// reported by Unpaywall and Europe PMC.
type LawfulFullTextResolver struct {
	Browser       *BrowserTransport
	Direct        *Transport
	Email         string
	UnpaywallBase string
	EuropePMCBase string
	URLAllowed    func(context.Context, string) bool
}

// NewLawfulFullTextResolver constructs the OA resolver.
func NewLawfulFullTextResolver(browser *BrowserTransport, direct *Transport, email string) *LawfulFullTextResolver {
	return &LawfulFullTextResolver{
		Browser: browser, Direct: direct, Email: strings.TrimSpace(email),
		UnpaywallBase: defaultUnpaywallBase, EuropePMCBase: defaultEuropePMCBase,
		URLAllowed: publicHTTPURL,
	}
}

// Resolve appends every usable lawful candidate in discovery order, retaining
// a merge only when it increases the normalized text length.
func (r *LawfulFullTextResolver) Resolve(ctx context.Context, raw *RawArticle, existing string) string {
	best := NormalizeScholarly(existing, -1)
	if r == nil || raw == nil || strings.TrimSpace(raw.DOI) == "" {
		return best
	}
	seenText := map[string]bool{best: true}
	for i, candidate := range r.candidateURLs(ctx, raw.DOI) {
		if i >= maxOACandidates || utf8.RuneCountInString(best) >= maxResolvedTextRunes {
			break
		}
		text := r.fetchURLText(ctx, raw.SourceKey, candidate, ocrLanguage(raw.Language))
		if text == "" || seenText[text] {
			continue
		}
		seenText[text] = true
		text = truncateTextRunes(text, maxResolvedTextRunes-utf8.RuneCountInString(best))
		merged := NormalizeScholarly(strings.TrimSpace(best+" "+text), -1)
		if utf8.RuneCountInString(merged) > utf8.RuneCountInString(best) {
			best = merged
		}
	}
	return best
}

func (r *LawfulFullTextResolver) candidateURLs(ctx context.Context, doi string) []string {
	urls := append(r.unpaywallURLs(ctx, doi), r.europePMCURLs(ctx, doi)...)
	seen := make(map[string]bool, len(urls))
	unique := make([]string, 0, len(urls))
	for _, candidate := range urls {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" || seen[candidate] {
			continue
		}
		seen[candidate] = true
		unique = append(unique, candidate)
	}
	return unique
}

type oaLocation struct {
	URLForPDF         string `json:"url_for_pdf"`
	URLForLandingPage string `json:"url_for_landing_page"`
	URL               string `json:"url"`
	PDFURL            string `json:"pdf_url"`
}

func (r *LawfulFullTextResolver) unpaywallURLs(ctx context.Context, doi string) []string {
	if r.Direct == nil || r.Email == "" {
		return nil
	}
	var payload struct {
		Best        *oaLocation  `json:"best_oa_location"`
		OALocations []oaLocation `json:"oa_locations"`
	}
	u := strings.TrimRight(r.UnpaywallBase, "/") + "/" + url.QueryEscape(doi) + "?email=" + url.QueryEscape(r.Email)
	if err := r.Direct.GetJSON(ctx, "unpaywall", u, map[string]string{"Accept": "application/json"}, &payload); err != nil {
		return nil
	}
	locations := make([]oaLocation, 0, len(payload.OALocations)+1)
	if payload.Best != nil {
		locations = append(locations, *payload.Best)
	}
	locations = append(locations, payload.OALocations...)
	var out []string
	for _, location := range locations {
		for _, candidate := range []string{location.URLForPDF, location.URLForLandingPage, location.URL, location.PDFURL} {
			if strings.HasPrefix(strings.TrimSpace(candidate), "http") {
				out = append(out, strings.TrimSpace(candidate))
			}
		}
	}
	return out
}

func (r *LawfulFullTextResolver) europePMCURLs(ctx context.Context, doi string) []string {
	if r.Direct == nil {
		return nil
	}
	var payload struct {
		ResultList struct {
			Results []struct {
				FullTextURLList struct {
					URLs []struct {
						URL string `json:"url"`
					} `json:"fullTextUrl"`
				} `json:"fullTextUrlList"`
			} `json:"result"`
		} `json:"resultList"`
	}
	u := r.EuropePMCBase + "?query=DOI:%22" + url.QueryEscape(doi) + "%22&format=json&pageSize=1&resultType=core"
	if err := r.Direct.GetJSON(ctx, "europe_pmc", u, map[string]string{"Accept": "application/json"}, &payload); err != nil || len(payload.ResultList.Results) == 0 {
		return nil
	}
	var out []string
	for _, item := range payload.ResultList.Results[0].FullTextURLList.URLs {
		if strings.HasPrefix(strings.TrimSpace(item.URL), "http") {
			out = append(out, strings.TrimSpace(item.URL))
		}
	}
	return out
}

func (r *LawfulFullTextResolver) fetchURLText(ctx context.Context, sourceKey, candidate, ocrLang string) string {
	if r.Browser == nil || r.URLAllowed == nil || !r.URLAllowed(ctx, candidate) {
		return ""
	}
	page, err := r.Browser.Fetch(ctx, sourceKey, candidate, nil,
		"text/html,application/xhtml+xml,application/pdf,*/*", 0)
	if err != nil {
		return ""
	}
	body := []byte(page.Body)
	if isPDFResponse(candidate, page.ContentType, body) {
		text, err := r.Browser.PDFText(ctx, sourceKey, body, ocrLang)
		if err == nil && text != "" {
			return NormalizeScholarly(text, -1)
		}
	}
	root, err := parseHTMLBody(page.Body)
	if err != nil {
		return ""
	}
	sanitizeHTML(root)
	text := htmlText(root)
	if pdfURL := extractPDFURL(root, candidate, page.Body, text); pdfURL != "" && pdfURL != candidate {
		if !r.URLAllowed(ctx, pdfURL) {
			return NormalizeScholarly(text, -1)
		}
		if pdfText, err := requestPDFText(ctx, r.Browser, sourceKey, pdfURL, ocrLang); err == nil && pdfText != "" {
			return NormalizeScholarly(pdfText, -1)
		}
	}
	return NormalizeScholarly(text, -1)
}

func publicHTTPURL(ctx context.Context, rawURL string) bool {
	parsed, err := url.Parse(rawURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil {
		return false
	}
	host := strings.TrimSpace(parsed.Hostname())
	if host == "" || strings.EqualFold(host, "localhost") || strings.HasSuffix(strings.ToLower(host), ".local") {
		return false
	}
	addresses, err := net.DefaultResolver.LookupIP(ctx, "ip", host)
	if err != nil || len(addresses) == 0 {
		return false
	}
	for _, address := range addresses {
		if address.IsLoopback() || address.IsPrivate() || address.IsLinkLocalUnicast() ||
			address.IsLinkLocalMulticast() || address.IsUnspecified() || address.IsMulticast() {
			return false
		}
	}
	return true
}

func truncateTextRunes(value string, max int) string {
	if max <= 0 {
		return ""
	}
	runes := []rune(value)
	if len(runes) <= max {
		return value
	}
	return string(runes[:max])
}
