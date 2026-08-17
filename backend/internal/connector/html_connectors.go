package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

// HTML-mode connectors: parity with the Django connectors in
// html_connectors.py. The HTMLEngine dispatches fetch/enrich to the per-source
// implementations, mixing the browser sidecar transport (request helpers from
// htmlmode.go) with direct HTTP where Django uses a plain async transport.

// HTMLEngine runs the HTML-mode connectors.
type HTMLEngine struct {
	Browser  *BrowserTransport
	Direct   *Transport
	Resolver *LawfulFullTextResolver
}

// NewHTMLEngine builds an engine from the browser sidecar URL.
func NewHTMLEngine(browserURL string) *HTMLEngine {
	return &HTMLEngine{
		Browser: NewBrowserTransport(browserURL),
		Direct:  NewTransport(),
	}
}

// Fetch dispatches to the per-source fetch implementation.
func (e *HTMLEngine) Fetch(ctx context.Context, sourceKey, query string, limit int) ([]RawArticle, error) {
	switch sourceKey {
	case "cinii":
		return e.fetchCiNii(ctx, query, limit)
	case "sciengine":
		return e.fetchSciEngine(ctx, query, limit)
	case "cyberleninka":
		return e.fetchCyberLeninka(ctx, query, limit)
	case "mathnet":
		return e.fetchMathNet(ctx, query, limit)
	case "scielo":
		return e.fetchSciELO(ctx, query, limit)
	case "persee":
		return e.fetchPersee(ctx, query, limit)
	case "openedition":
		return e.fetchOpenEdition(ctx, query, limit)
	case "dergipark":
		return e.fetchDergiPark(ctx, query, limit)
	case "hrcak":
		return e.fetchHrcak(ctx, query, limit)
	case "ajol":
		return e.fetchAJOL(ctx, query, limit)
	default:
		return nil, fetchErr(sourceKey, "not an html-mode connector")
	}
}

// Enrich dispatches to the per-source enrichment; sources without a custom
// implementation use the base-class enrich_raw behavior.
func (e *HTMLEngine) Enrich(ctx context.Context, raw *RawArticle) (*RawArticle, error) {
	switch raw.SourceKey {
	case "cyberleninka":
		return e.enrichCyberLeninka(ctx, raw)
	case "mathnet":
		return e.enrichMathNet(ctx, raw)
	case "scielo":
		return e.enrichSciELO(ctx, raw)
	case "ajol":
		return e.enrichAJOL(ctx, raw)
	case "openedition":
		return e.enrichRawBase(ctx, raw, []string{
			"citation_journal_title", "citation_inbook_title", "citation_abstract",
		})
	case "dergipark", "hrcak":
		// Django parity: the OAI payload is authoritative, and base enrichment
		// would inventory unrelated page badges as a bogus DOI.
		return raw, nil
	default:
		return e.enrichRawBase(ctx, raw, nil)
	}
}

// ---------------------------------------------------------------------------
// CiNii (OpenSearch JSON, HTML fallback).
// ---------------------------------------------------------------------------

func (e *HTMLEngine) fetchCiNii(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	apiURL := "https://cir.nii.ac.jp/opensearch/v2/articles" +
		"?format=json&q=" + quotePlus(query) + "&lang=en&count=" + strconv.Itoa(limit)
	payload, err := requestJSON(ctx, e.Browser, "cinii", apiURL, "application/json,*/*")
	if err != nil {
		if isConnectorError(err) {
			// Django catches ConnectorFetchError and falls back to HTML.
			return fetchHTMLGeneric(ctx, e.Browser, mustProfile("cinii"), query, limit)
		}
		return nil, err
	}
	return e.extractCiNiiPayload(query, payload, limit), nil
}

func (e *HTMLEngine) extractCiNiiPayload(query string, payload map[string]any, limit int) []RawArticle {
	rawEntries, _ := payload["items"].([]any)
	items := make([]RawArticle, 0, limit)
	for i, entry := range rawEntries {
		if i >= limit {
			break
		}
		m, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		title := strings.TrimSpace(fmt.Sprint(m["title"]))
		urlValue := ""
		switch link := m["link"].(type) {
		case map[string]any:
			urlValue = strings.TrimSpace(firstMapString(link, "@id", "id"))
		case string:
			urlValue = strings.TrimSpace(link)
		}
		if title == "" || urlValue == "" {
			continue
		}
		journal := extractCiNiiJournal(m)
		abstract := strings.TrimSpace(fmt.Sprint(m["description"]))
		authors := extractCiNiiAuthors(m)
		doi := extractCiNiiDOI(m)
		if doi == "" {
			doi = ExtractDOI(title + " " + abstract)
		}
		year := ExtractYear(firstMapString(m, "prism:publicationDate", "dc:date"))
		if year == 0 {
			year = ExtractYear(title + " " + abstract + " " + journal)
		}
		combined := strings.Join([]string{title, abstract, strings.Join(authors, " "), journal}, " ")
		items = append(items, buildRaw(mustProfile("cinii"), title, urlValue, abstract, combined,
			doi, journal, year, authors, "", "", "", inferCiNiiLanguage(title+" "+abstract)))
	}
	return items
}

// firstMapString returns the first non-empty string value for the keys.
func firstMapString(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k].(string); ok && strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

// extractCiNiiJournal prefers prism:publicationName, then dc:publisher, then a
// string-valued dc:source (dict/list shapes render as garbage and are skipped).
func extractCiNiiJournal(m map[string]any) string {
	for _, key := range []string{"prism:publicationName", "dc:publisher"} {
		if v, ok := m[key].(string); ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	switch source := m["dc:source"].(type) {
	case string:
		if strings.TrimSpace(source) != "" {
			return strings.TrimSpace(source)
		}
	case []any:
		for _, item := range source {
			if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
				return strings.TrimSpace(s)
			}
		}
	}
	return "CiNii"
}

// extractCiNiiAuthors returns the dc:creator list of names.
func extractCiNiiAuthors(m map[string]any) []string {
	switch creator := m["dc:creator"].(type) {
	case string:
		if strings.TrimSpace(creator) != "" {
			return []string{strings.TrimSpace(creator)}
		}
	case []any:
		var out []string
		for _, c := range creator {
			if s := strings.TrimSpace(fmt.Sprint(c)); s != "" {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

// extractCiNiiDOI returns the DOI from a cir:DOI dc:identifier entry.
func extractCiNiiDOI(m map[string]any) string {
	identifiers, ok := m["dc:identifier"].([]any)
	if !ok {
		return ""
	}
	for _, ident := range identifiers {
		im, ok := ident.(map[string]any)
		if !ok {
			continue
		}
		if strings.ToLower(fmt.Sprint(im["@type"])) == "cir:doi" {
			if v := strings.TrimSpace(fmt.Sprint(im["@value"])); v != "" {
				return v
			}
		}
	}
	return ""
}

// inferCiNiiLanguage detects Japanese script (parity with
// _infer_cinii_language: kana and Han ideographs resolve to "ja", Latin-only
// text resolves to "").
func inferCiNiiLanguage(text string) string {
	for _, r := range text {
		if (r >= 0x3040 && r <= 0x30FF) ||
			(r >= 0x4E00 && r <= 0x9FFF) ||
			(r >= 0xFF66 && r <= 0xFF9F) {
			return "ja"
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// SciEngine (POST /SciSearch/searchNew JSON).
// ---------------------------------------------------------------------------

const (
	sciEnginePageSize = 10
	sciEngineMaxPages = 20
)

func (e *HTMLEngine) fetchSciEngine(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	var records []any
	page := 1
	for len(records) < limit && page <= sciEngineMaxPages {
		payload, err := e.requestSciEnginePage(ctx, query, page)
		if err != nil {
			return nil, err
		}
		relate, _ := payload["relateList"].([]any)
		if len(relate) == 0 {
			break
		}
		records = append(records, relate...)
		if len(relate) < sciEnginePageSize {
			break
		}
		page++
	}
	return e.extractSciEnginePayload(records, limit), nil
}

func (e *HTMLEngine) requestSciEnginePage(ctx context.Context, query string, page int) (map[string]any, error) {
	pageResult, err := e.Browser.PostForm(ctx, "sciengine", mustProfile("sciengine").SearchURL, map[string]string{
		"queryField_a": query,
		"searchType":   "all",
		"curpage":      strconv.Itoa(page),
		"dept":         strconv.Itoa(sciEnginePageSize),
	}, "application/json, text/plain, */*")
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(pageResult.Body), &payload); err != nil {
		return nil, fetchErr("sciengine", "invalid api json: %v", err)
	}
	if payload == nil {
		return nil, fetchErr("sciengine", "invalid api json type")
	}
	return payload, nil
}

func (e *HTMLEngine) extractSciEnginePayload(records []any, limit int) []RawArticle {
	items := make([]RawArticle, 0, limit)
	for i, rec := range records {
		if i >= limit*3 {
			break
		}
		m, ok := rec.(map[string]any)
		if !ok {
			continue
		}
		title := strings.TrimSpace(firstMapString(m, "title_en", "title_cn"))
		doi := strings.TrimSpace(fmt.Sprint(m["doi"]))
		if title == "" || doi == "" {
			continue
		}
		urlValue := "https://doi.org/" + doi
		abstract := StripHTMLTags(firstMapString(m, "intro_en", "intro_cn"))
		year := ExtractYear(firstMapString(m, "pubYear", "pubDateStr"))
		authors := extractSciEngineAuthors(m)
		combined := strings.Join([]string{title, abstract, strings.Join(authors, " "), "SciEngine"}, " ")
		if !IsArticleLikeItem(title, urlValue, doi, year) {
			continue
		}
		items = append(items, buildRaw(mustProfile("sciengine"), title, urlValue, abstract, combined,
			doi, "SciEngine", year, authors, "", "", "", ""))
		if len(items) >= limit {
			break
		}
	}
	return items
}

// extractSciEngineAuthors returns the fullname_en/fullname_cn author values.
func extractSciEngineAuthors(m map[string]any) []string {
	for _, key := range []string{"fullname_en", "fullname_cn"} {
		switch v := m[key].(type) {
		case string:
			if strings.TrimSpace(v) != "" {
				return []string{strings.TrimSpace(v)}
			}
		case []any:
			var names []string
			for _, a := range v {
				if s := strings.TrimSpace(fmt.Sprint(a)); s != "" {
					names = append(names, s)
				}
			}
			if len(names) > 0 {
				return names
			}
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// CyberLeninka (POST /api/search JSON; HTML fallback chain).
// ---------------------------------------------------------------------------

func (e *HTMLEngine) fetchCyberLeninka(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	articles, err := e.fetchCyberLeninkaAPI(ctx, query, limit)
	if err != nil {
		if isConnectorError(err) {
			return e.fetchCyberLeninkaHTML(ctx, query, limit)
		}
		return nil, err
	}
	return articles, nil
}

func (e *HTMLEngine) fetchCyberLeninkaAPI(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	size := max(3, limit*2)
	pageResult, err := e.Browser.PostJSON(ctx, "cyberleninka", "https://cyberleninka.ru/api/search", map[string]any{
		"q":    query,
		"size": size,
		"from": 0,
		"mode": "articles",
	})
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(pageResult.Body), &payload); err != nil {
		return nil, fetchErr("cyberleninka", "invalid api json: %v", err)
	}
	if payload == nil {
		return nil, fetchErr("cyberleninka", "invalid api json type")
	}
	return extractCyberLeninkaPayload(mustProfile("cyberleninka"), payload, limit), nil
}

func extractCyberLeninkaPayload(profile SourceProfile, payload map[string]any, limit int) []RawArticle {
	records, _ := payload["articles"].([]any)
	items := make([]RawArticle, 0, limit)
	for i, rec := range records {
		if i >= limit*3 {
			break
		}
		m, ok := rec.(map[string]any)
		if !ok {
			continue
		}
		title := StripHTMLTags(fmt.Sprint(m["name"]))
		abstract := StripHTMLTags(fmt.Sprint(m["annotation"]))
		href := strings.TrimSpace(fmt.Sprint(m["link"]))
		urlValue := resolveURL(profile.SearchURL, href)
		year := ExtractYear(fmt.Sprint(m["year"]))
		journal := strings.TrimSpace(fmt.Sprint(m["journal"]))
		if journal == "" {
			journal = "CyberLeninka"
		}
		doi := ExtractDOI(title + " " + abstract + " " + journal)
		var authors []string
		if rawAuthors, ok := m["authors"].([]any); ok {
			for _, a := range rawAuthors {
				if s, ok := a.(string); ok && strings.TrimSpace(s) != "" {
					authors = append(authors, strings.TrimSpace(s))
				}
			}
		}
		combined := strings.Join([]string{title, abstract, journal, strings.Join(authors, " ")}, " ")
		if title == "" || !strings.HasPrefix(urlValue, "http") {
			continue
		}
		if !IsArticleLikeItem(title, urlValue, doi, year) {
			continue
		}
		items = append(items, buildRaw(profile, title, urlValue, abstract, combined,
			doi, journal, year, authors, "", "", "", ""))
		if len(items) >= limit {
			break
		}
	}
	return items
}

func (e *HTMLEngine) fetchCyberLeninkaHTML(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	profile := mustProfile("cyberleninka")
	htmlBody, err := requestText(ctx, e.Browser, "cyberleninka", profile.SearchURL,
		map[string]string{"q": query}, "", 0, ocrLanguage(profile.Language))
	if err != nil {
		return nil, err
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return nil, fetchErr("cyberleninka", "invalid HTML: %v", err)
	}
	if err := assertPageParseable("cyberleninka", htmlBody, root); err != nil {
		return nil, err
	}
	rows := selectNodes(root, ".article-item, .articles-list .item, .content-list__item, article, li")
	items := make([]RawArticle, 0, limit)
	for _, row := range rows {
		titleNodes := selectNodes(row, ".title a, h2 a, h3 a, a[href]")
		if len(titleNodes) == 0 {
			continue
		}
		title := strings.TrimSpace(nodeText(titleNodes[0]))
		href := resolveURL(profile.SearchURL, nodeAttr(titleNodes[0], "href"))
		abstract := ""
		if absNodes := selectNodes(row, ".annotation, .description, .abstract, p"); len(absNodes) > 0 {
			abstract = strings.TrimSpace(nodeText(absNodes[0]))
		}
		journal := strings.ToUpper(profile.SourceKey)
		if jNodes := selectNodes(row, ".journal, .publication, .source, .subtitle"); len(jNodes) > 0 {
			if j := strings.TrimSpace(nodeText(jNodes[0])); j != "" {
				journal = j
			}
		}
		combined := strings.Join([]string{title, abstract, journal, nodeText(row)}, " ")
		doi := ExtractDOI(combined)
		year := ExtractYear(combined)
		if !IsArticleLikeItem(title, href, doi, year) {
			continue
		}
		items = append(items, buildRaw(profile, title, href, abstract, combined,
			doi, journal, year, nil, "", "", "", ""))
		if len(items) >= limit {
			break
		}
	}
	if len(items) > 0 {
		return items, nil
	}
	if jsonLd := extractJSONLDArticles(profile, root, limit); len(jsonLd) > 0 {
		return jsonLd, nil
	}
	return genericExtractFromHTML(profile, root, limit), nil
}

// enrichCyberLeninka backfills an empty abstract from the article-body block
// (parity with CyberLeninkaConnector.enrich_raw: only when the base enrichment
// left the abstract empty; failures degrade to the enriched raw unchanged).
func (e *HTMLEngine) enrichCyberLeninka(ctx context.Context, raw *RawArticle) (*RawArticle, error) {
	enriched, err := e.enrichRawBase(ctx, raw, nil)
	if err != nil {
		return nil, err
	}
	if enriched == nil {
		//nolint:nilnil // Django parity: non-article landing pages drop the record
		return nil, nil
	}
	if strings.TrimSpace(enriched.Abstract) != "" {
		return enriched, nil
	}
	if !strings.HasPrefix(raw.URL, "http") || strings.TrimSpace(raw.Title) == "" {
		return enriched, nil
	}
	htmlBody, err := requestText(ctx, e.Browser, "cyberleninka", raw.URL, nil, "", 0, ocrLanguage(raw.Language))
	if err != nil {
		if isConnectorError(err) {
			return enriched, nil // Django parity: ConnectorFetchError degrades to the enriched record
		}
		return enriched, err
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return enriched, nil //nolint:nilerr // Django parses leniently; degrade rather than fail the record
	}
	sanitizeHTML(root)
	ocrBlocks := selectNodes(root, "div.ocr[itemprop='articleBody']")
	if len(ocrBlocks) == 0 {
		return enriched, nil
	}
	var paragraphs []string
	for _, p := range selectNodes(ocrBlocks[0], "p") {
		paragraphs = append(paragraphs, strings.TrimSpace(nodeText(p)))
	}
	abstract := extractCyberLeninkaAbstract(paragraphs, raw.Title)
	if abstract == "" {
		return enriched, nil
	}
	out := *enriched
	out.Abstract = NormalizeScholarly(abstract, 8000)
	return &out, nil
}

// ---------------------------------------------------------------------------
// Persée (HTML rows).
// ---------------------------------------------------------------------------

var perseeCitationRe = regexp.MustCompile(`(?i)\bPour citer cet article\b`)

func (e *HTMLEngine) fetchPersee(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	profile := mustProfile("persee")
	htmlBody, err := requestText(ctx, e.Browser, "persee", profile.SearchURL,
		map[string]string{"q": query}, "", 0, ocrLanguage(profile.Language))
	if err != nil {
		return nil, err
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return nil, fetchErr("persee", "invalid HTML: %v", err)
	}
	if err := assertPageParseable("persee", htmlBody, root); err != nil {
		return nil, err
	}
	items := make([]RawArticle, 0, limit)
	for _, node := range selectNodes(root, ".doc-result") {
		titleNodes := selectNodes(node, "a.title")
		if len(titleNodes) == 0 {
			continue
		}
		title := strings.TrimSpace(nodeText(titleNodes[0]))
		urlValue := strings.Split(nodeAttr(titleNodes[0], "href"), "?")[0]
		if title == "" || urlValue == "" {
			continue
		}
		var authors []string
		for _, n := range selectNodes(node, ".contributors .name") {
			if t := strings.TrimSpace(nodeText(n)); t != "" {
				authors = append(authors, t)
			}
		}
		journal := "Persee"
		if jNodes := selectNodes(node, ".documentBibRef .collection a"); len(jNodes) > 0 {
			if j := strings.TrimSpace(nodeText(jNodes[0])); j != "" {
				journal = j
			}
		}
		nodeTextAll := nodeText(node)
		year := ExtractYear(nodeTextAll)
		if year == 0 {
			year = ExtractYear(urlValue)
		}
		abstract := ""
		if absNodes := selectNodes(node, ".searchContext"); len(absNodes) > 0 {
			abstract = strings.TrimSpace(nodeText(absNodes[0]))
		}
		// .searchContext trails into the "Pour citer cet article" citation
		// block; strip from that marker so the fallback abstract is the
		// abstract fragment, not the bibliography.
		if parts := perseeCitationRe.Split(abstract, 2); len(parts) > 0 {
			abstract = strings.TrimSpace(parts[0])
		}
		doi := ExtractDOI(nodeTextAll)
		combined := strings.Join([]string{title, abstract, strings.Join(authors, " "), journal, urlValue}, " ")
		if !IsArticleLikeItem(title, urlValue, doi, year) {
			continue
		}
		items = append(items, buildRaw(profile, title, urlValue, abstract, combined,
			doi, journal, year, authors, "", "", "", ""))
		if len(items) >= limit {
			break
		}
	}
	return items, nil
}

// ---------------------------------------------------------------------------
// OpenEdition (RSS via direct HTTP; landing-page enrichment via sidecar).
// ---------------------------------------------------------------------------

// openeditionHosts are the DOI-bearing article hosts.
var openeditionHosts = map[string]bool{
	"journals.openedition.org": true,
	"books.openedition.org":    true,
}

func (e *HTMLEngine) fetchOpenEdition(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	var items []RawArticle
	seen := make(map[string]bool)
	var lastErr error
	for _, platform := range []string{"OJ", "OB"} {
		if len(items) >= limit {
			break
		}
		u := "https://search-api.openedition.org/rss?q=" + quotePlus(query) +
			"&mm=100&platform=" + platform
		xmlText, err := e.Direct.GetText(ctx, "openedition", u, "application/rss+xml,application/xml,*/*")
		if err != nil {
			if isConnectorError(err) {
				lastErr = err
				continue
			}
			return nil, err
		}
		for _, raw := range e.parseOpenEditionItems(xmlText) {
			if raw.URL == "" || seen[raw.URL] {
				continue
			}
			seen[raw.URL] = true
			items = append(items, raw)
			if len(items) >= limit {
				break
			}
		}
	}
	if len(items) == 0 && lastErr != nil {
		return nil, lastErr
	}
	if len(items) > limit {
		items = items[:limit]
	}
	return items, nil
}

func (e *HTMLEngine) parseOpenEditionItems(xmlText string) []RawArticle {
	items, err := parseFeed(xmlText)
	if err != nil {
		return nil
	}
	profile := mustProfile("openedition")
	var out []RawArticle
	for _, item := range items {
		title := strings.TrimSpace(item.Title)
		urlValue := strings.TrimSpace(item.Link)
		if title == "" || urlValue == "" {
			continue
		}
		doi := deriveOpenEditionDOI(urlValue)
		var year int
		if item.PubDate != "" {
			year = ExtractYear(item.PubDate)
		}
		authors := parseOpenEditionAuthors(item.Creators)
		if !isTrueOpenEditionArticle(urlValue, doi, title, item.Description) {
			continue
		}
		if !IsArticleLikeItem(title, urlValue, doi, year) {
			continue
		}
		description := strings.TrimSpace(item.Description)
		out = append(out, buildRaw(profile, title, urlValue, description,
			title+" "+description, doi, profile.SourceKey, year, authors, "", "", "", ""))
	}
	return out
}

// deriveOpenEditionDOI reconstructs the deterministic 10.4000/<slug>.<id> DOI.
func deriveOpenEditionDOI(u string) string {
	parsed, err := url.Parse(u)
	if err != nil {
		return ""
	}
	if !openeditionHosts[strings.ToLower(parsed.Host)] {
		return ""
	}
	m := regexp.MustCompile(`^/([^/]+)/(\d+)/?$`).FindStringSubmatch(parsed.Path)
	if m == nil {
		return ""
	}
	return "10.4000/" + m[1] + "." + m[2]
}

// parseOpenEditionAuthors pairs consecutive "Surname, Firstname" tokens and
// reverses each pair (parity with _parse_openedition_authors).
func parseOpenEditionAuthors(creators []string) []string {
	var tokens []string
	for _, c := range creators {
		for _, t := range strings.Split(c, ",") {
			if s := strings.TrimSpace(t); s != "" {
				tokens = append(tokens, s)
			}
		}
	}
	var authors []string
	for i := 0; i < len(tokens); i += 2 {
		if i+1 < len(tokens) {
			authors = append(authors, tokens[i+1]+" "+tokens[i])
		} else {
			authors = append(authors, tokens[i])
		}
	}
	return authors
}

// isTrueOpenEditionArticle rejects blog/event records and accepts only
// journal-article / book-chapter URLs and DOIs (parity with
// _is_true_article_record).
func isTrueOpenEditionArticle(u, doi, title, description string) bool {
	loweredURL := strings.ToLower(u)
	loweredDOI := strings.ToLower(doi)
	loweredTitle := strings.ToLower(title)
	loweredDescription := strings.ToLower(description)
	if strings.HasPrefix(loweredDOI, "10.58079/") {
		return false
	}
	if strings.Contains(loweredURL, "hypotheses.org") {
		return false
	}
	if strings.Contains(loweredTitle, "blog") || strings.Contains(loweredDescription, "blog") {
		return false
	}
	parsed, err := url.Parse(u)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return false
	}
	return strings.Contains(loweredURL, "journals.openedition.org") ||
		strings.Contains(loweredURL, "books.openedition.org") ||
		strings.Contains(loweredURL, "doi.org/10.4000/") ||
		strings.HasPrefix(loweredDOI, "10.4000/")
}

// mustProfile returns the profile for a key, panicking on unknown keys (the
// keys are compile-time constants in this file).
func mustProfile(key string) SourceProfile {
	p, err := ProfileFor(key)
	if err != nil {
		panic(err)
	}
	return p
}
