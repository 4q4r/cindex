package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"golang.org/x/net/html"
)

// Shared HTML-mode machinery, parity with the BaseConnector helpers in
// apps.ingestion.connectors.base (request helpers, generic HTML extraction,
// meta extraction, JSON-LD extraction, landing-page enrichment) plus the mini
// CSS selector engine used by the HTML connectors.

// ocrLanguageMap mirrors OCR_LANGUAGE_MAP in base.py.
var ocrLanguageMap = map[string]string{
	"ar": "ara", "de": "deu", "en": "eng", "eng": "eng", "es": "spa",
	"fr": "fra", "it": "ita", "ja": "jpn", "jpn": "jpn", "ko": "kor",
	"kor": "kor", "pt": "por", "ru": "rus", "rus": "rus",
	"zh": "chi_sim+chi_tra", "zho": "chi_sim+chi_tra",
}

// ocrLanguage maps a record language to a Tesseract OCR code (parity with
// BaseConnector._ocr_language: map lookup, then 2-letter fallback, else eng).
func ocrLanguage(language string) string {
	n := strings.ToLower(NormalizeScholarly(language, -1))
	if n == "" {
		return "eng"
	}
	if v, ok := ocrLanguageMap[n]; ok {
		return v
	}
	if len(n) >= 2 {
		if v, ok := ocrLanguageMap[n[:2]]; ok {
			return v
		}
	}
	return "eng"
}

// isPDFResponse detects a PDF response by URL, content type, or magic bytes
// (parity with BaseConnector._is_pdf_response).
func isPDFResponse(u, contentType string, body []byte) bool {
	ct := strings.ToLower(contentType)
	ul := strings.ToLower(u)
	return strings.HasSuffix(ul, ".pdf") ||
		strings.Contains(ct, "application/pdf") ||
		bytes.HasPrefix(body, []byte("%PDF"))
}

// requestText fetches a text resource (HTML/XML/RSS/JSON) through the browser
// sidecar. PDF responses are routed to the PDF text extractor; a residual
// challenge page is a terminal FetchError (parity with _request_text).
func requestText(ctx context.Context, bt *BrowserTransport, sourceKey, u string, params map[string]string, accept string, timeoutSeconds float64, ocrLang string) (string, error) {
	page, err := bt.Fetch(ctx, sourceKey, u, params, accept, timeoutSeconds)
	if err != nil {
		return "", err
	}
	body := []byte(page.Body)
	if isPDFResponse(u, page.ContentType, body) {
		text, _ := bt.PDFText(ctx, sourceKey, body, ocrLang)
		return text, nil
	}
	if IsChallengePage(page.Body) {
		return "", fetchErr(sourceKey, "challenge page unresolved")
	}
	return page.Body, nil
}

// requestPDFText fetches a PDF resource and extracts its text (parity with
// _request_pdf_text: non-PDF bodies are normalized and returned as text).
func requestPDFText(ctx context.Context, bt *BrowserTransport, sourceKey, u, ocrLang string) (string, error) {
	page, err := bt.Fetch(ctx, sourceKey, u, nil, "application/pdf,*/*", 0)
	if err != nil {
		return "", err
	}
	body := []byte(page.Body)
	if isPDFResponse(u, page.ContentType, body) {
		text, _ := bt.PDFText(ctx, sourceKey, body, ocrLang)
		return text, nil
	}
	if page.Body != "" {
		return NormalizeScholarly(page.Body, -1), nil
	}
	return "", nil
}

// requestJSON fetches a JSON object through the sidecar (parity with
// _request_json: a non-object payload is a terminal contract violation).
func requestJSON(ctx context.Context, bt *BrowserTransport, sourceKey, u, accept string) (map[string]any, error) {
	page, err := bt.Fetch(ctx, sourceKey, u, nil, accept, 0)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(page.Body), &payload); err != nil {
		return nil, fetchErr(sourceKey, "invalid JSON payload: %v", err)
	}
	if payload == nil {
		return nil, fetchErr(sourceKey, "invalid JSON payload type")
	}
	return payload, nil
}

// ---------------------------------------------------------------------------
// Mini CSS selector engine (subset sufficient for the connector selectors).
// ---------------------------------------------------------------------------

type cssSegment struct {
	tag       string
	class     string
	attr      string
	attrVal   string
	hasAttr   bool
	attrEqual bool // false => contains
}

// parseCSSSelector splits a comma-separated selector list into segment chains.
func parseCSSSelector(selector string) [][]cssSegment {
	var out [][]cssSegment
	for _, group := range strings.Split(selector, ",") {
		var segs []cssSegment
		for _, part := range strings.Fields(group) {
			seg, ok := parseCSSSegment(part)
			if !ok {
				segs = nil
				break
			}
			segs = append(segs, seg)
		}
		if len(segs) > 0 {
			out = append(out, segs)
		}
	}
	return out
}

var cssTagRe = regexp.MustCompile(`^[a-zA-Z][a-zA-Z0-9]*`)

func parseCSSSegment(s string) (cssSegment, bool) {
	var seg cssSegment
	rest := s
	if m := cssTagRe.FindString(rest); m != "" {
		seg.tag = m
		rest = rest[len(m):]
	}
	for rest != "" {
		switch rest[0] {
		case '.':
			i := strings.IndexAny(rest[1:], ".[")
			if i < 0 {
				i = len(rest[1:])
			}
			seg.class = rest[1 : 1+i]
			rest = rest[1+i:]
		case '[':
			end := strings.IndexByte(rest, ']')
			if end < 0 {
				return cssSegment{}, false
			}
			inner := rest[1:end]
			rest = rest[end+1:]
			eq := strings.IndexByte(inner, '=')
			if eq < 0 {
				seg.hasAttr = true
				seg.attr = strings.TrimSpace(inner)
				seg.attrEqual = true
				continue
			}
			name := strings.TrimSpace(inner[:eq])
			value := strings.TrimSpace(inner[eq+1:])
			contains := strings.HasSuffix(name, "*")
			if contains {
				name = strings.TrimSpace(strings.TrimSuffix(name, "*"))
			}
			value = strings.Trim(value, `"'`)
			seg.hasAttr = true
			seg.attr = name
			seg.attrVal = value
			seg.attrEqual = !contains
		default:
			return cssSegment{}, false
		}
	}
	if seg.tag == "" && seg.class == "" && !seg.hasAttr {
		return cssSegment{}, false
	}
	return seg, true
}

func matchCSSSegment(n *html.Node, seg cssSegment) bool {
	if n.Type != html.ElementNode {
		return false
	}
	if seg.tag != "" && n.Data != seg.tag {
		return false
	}
	if seg.class != "" && !hasClass(n, seg.class) {
		return false
	}
	if seg.hasAttr {
		v := nodeAttr(n, seg.attr)
		if seg.attrEqual {
			if v != seg.attrVal {
				return false
			}
		} else if !strings.Contains(strings.ToLower(v), strings.ToLower(seg.attrVal)) {
			return false
		}
	}
	return true
}

// selectNodes returns nodes matching a comma-separated selector list in
// document order, de-duplicated. Supported: tag, .class, tag.class,
// tag[attr], tag[attr='value'], tag[attr*='value'], whitespace combinators.
func selectNodes(root *html.Node, selector string) []*html.Node {
	chains := parseCSSSelector(selector)
	var out []*html.Node
	for _, chain := range chains {
		var matches func(n *html.Node, i int) bool
		matches = func(n *html.Node, i int) bool {
			if !matchCSSSegment(n, chain[i]) {
				return false
			}
			if i == len(chain)-1 {
				return true
			}
			for c := n.FirstChild; c != nil; c = c.NextSibling {
				if matches(c, i+1) {
					return true
				}
			}
			return false
		}
		walkHTML(root, func(n *html.Node) bool {
			if matches(n, 0) {
				out = append(out, n)
			}
			return false
		})
	}
	if len(out) < 2 {
		return out
	}
	seen := make(map[*html.Node]bool, len(out))
	dedup := out[:0]
	for _, n := range out {
		if !seen[n] {
			seen[n] = true
			dedup = append(dedup, n)
		}
	}
	return dedup
}

// ---------------------------------------------------------------------------
// HTML sanitizing / text / meta helpers.
// ---------------------------------------------------------------------------

var htmlBoilerplateTags = map[string]bool{
	"script": true, "style": true, "noscript": true, "template": true,
	"svg": true, "canvas": true, "iframe": true,
}

// sanitizeHTML removes boilerplate tags (parity with _sanitize_html_soup).
func sanitizeHTML(root *html.Node) {
	var toRemove []*html.Node
	walkHTML(root, func(n *html.Node) bool {
		if n.Type == html.ElementNode && htmlBoilerplateTags[n.Data] {
			toRemove = append(toRemove, n)
		}
		return false
	})
	for _, n := range toRemove {
		if n.Parent != nil {
			n.Parent.RemoveChild(n)
		}
	}
}

// htmlText renders sanitized HTML text (parity with _html_text).
func htmlText(root *html.Node) string {
	var parts []string
	walkHTML(root, func(n *html.Node) bool {
		if n.Type == html.TextNode {
			if t := strings.TrimSpace(n.Data); t != "" {
				parts = append(parts, t)
			}
		}
		return false
	})
	return NormalizeScholarly(strings.Join(parts, " "), -1)
}

// extractMetaContent returns the first non-empty meta content for the keys,
// respecting key priority (parity with _extract_meta_content).
func extractMetaContent(root *html.Node, keys []string) string {
	metas := selectNodes(root, "meta[name], meta[property]")
	for _, key := range keys {
		target := strings.ToLower(key)
		for _, meta := range metas {
			name := strings.ToLower(strings.TrimSpace(nodeAttr(meta, "name")))
			if name == "" {
				name = strings.ToLower(strings.TrimSpace(nodeAttr(meta, "property")))
			}
			if name == target {
				if content := strings.TrimSpace(nodeAttr(meta, "content")); content != "" {
					return content
				}
			}
		}
	}
	return ""
}

// extractMetaText joins every meta content value (parity with _extract_meta_text).
func extractMetaText(root *html.Node) string {
	var values []string
	for _, meta := range selectNodes(root, "meta[name], meta[property]") {
		if content := strings.TrimSpace(nodeAttr(meta, "content")); content != "" {
			values = append(values, content)
		}
	}
	return strings.Join(values, " ")
}

// extractPDFURL finds a likely PDF URL from metadata, anchors, or raw text
// blobs (parity with _extract_pdf_url).
func extractPDFURL(root *html.Node, base string, blobs ...string) string {
	for _, key := range []string{
		"citation_pdf_url", "citation_pdfurl", "dc.identifier", "dc.source",
		"prism.url", "og:url",
	} {
		value := strings.TrimSpace(extractMetaContent(root, []string{key}))
		if strings.HasSuffix(strings.ToLower(value), ".pdf") && strings.HasPrefix(value, "http") {
			return value
		}
	}
	for _, link := range selectNodes(root, "a[href]") {
		href := resolveURL(base, nodeAttr(link, "href"))
		label := strings.ToLower(nodeText(link))
		hrefLower := strings.ToLower(href)
		if (strings.HasSuffix(hrefLower, ".pdf") || strings.Contains(hrefLower, "pdf") || strings.Contains(label, "pdf")) && strings.HasPrefix(href, "http") {
			return href
		}
	}
	for _, blob := range blobs {
		if found := ExtractPDFURL(blob); found != "" {
			return found
		}
	}
	return ""
}

// assertPageParseable raises a terminal error on challenge/404/empty pages
// (parity with _assert_page_is_parseable).
func assertPageParseable(sourceKey, rawHTML string, root *html.Node) error {
	text := strings.ToLower(htmlText(root))
	for _, marker := range []string{"verify you are human", "captcha"} {
		if strings.Contains(text, marker) {
			return fetchErr(sourceKey, "blocked by verification challenge")
		}
	}
	pageTitle := ""
	if title := selectNodes(root, "title"); len(title) > 0 {
		pageTitle = strings.ToLower(strings.TrimSpace(nodeText(title[0])))
	}
	if strings.Contains(pageTitle, "404") && strings.Contains(pageTitle, "not found") {
		return fetchErr(sourceKey, "search page not found")
	}
	if strings.TrimSpace(rawHTML) == "" {
		return fetchErr(sourceKey, "empty response body")
	}
	return nil
}

// ---------------------------------------------------------------------------
// Generic HTML row extraction (parity with _fetch_html + _extract_from_html +
// _build_from_row using the profile selectors).
// ---------------------------------------------------------------------------

// fetchHTMLGeneric performs the base-class HTML fetch: GET the search URL with
// the query param, parse, assert, and extract profile-selector rows.
func fetchHTMLGeneric(ctx context.Context, bt *BrowserTransport, profile SourceProfile, query string, limit int) ([]RawArticle, error) {
	htmlBody, err := requestText(ctx, bt, profile.SourceKey, profile.SearchURL,
		map[string]string{profile.QueryParam: query}, "", 0, ocrLanguage(profile.Language))
	if err != nil {
		return nil, err
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return nil, fetchErr(profile.SourceKey, "invalid HTML: %v", err)
	}
	if err := assertPageParseable(profile.SourceKey, htmlBody, root); err != nil {
		return nil, err
	}
	return genericExtractFromHTML(profile, root, limit), nil
}

// genericExtractFromHTML extracts rows by the profile selectors (parity with
// _extract_from_html + _build_from_row).
func genericExtractFromHTML(profile SourceProfile, root *html.Node, limit int) []RawArticle {
	rows := selectNodes(root, profile.ResultSelector)
	items := make([]RawArticle, 0, limit)
	for _, row := range rows {
		titleNodes := selectNodes(row, profile.TitleSelector)
		linkNodes := selectNodes(row, profile.LinkSelector)
		if len(titleNodes) == 0 || len(linkNodes) == 0 {
			continue
		}
		title := strings.TrimSpace(nodeText(titleNodes[0]))
		href := resolveURL(profile.SearchURL, nodeAttr(linkNodes[0], "href"))
		abstract := ""
		if absNodes := selectNodes(row, profile.AbstractSelector); len(absNodes) > 0 {
			abstract = strings.TrimSpace(nodeText(absNodes[0]))
		}
		journal := strings.ToUpper(profile.SourceKey)
		if jNodes := selectNodes(row, profile.JournalSelector); len(jNodes) > 0 {
			if j := strings.TrimSpace(nodeText(jNodes[0])); j != "" {
				journal = j
			}
		}
		combined := strings.Join([]string{title, abstract, journal}, " ")
		items = append(items, buildRaw(profile, title, href, abstract, combined,
			ExtractDOI(combined), journal, ExtractYear(combined), nil, "", "", "", ""))
		if len(items) >= limit {
			break
		}
	}
	return items
}

// ---------------------------------------------------------------------------
// JSON-LD article extraction (parity with _extract_json_ld_articles).
// ---------------------------------------------------------------------------

// extractJSONLDArticles extracts article records from application/ld+json
// scripts (parity with _extract_json_ld_articles + helpers).
func extractJSONLDArticles(profile SourceProfile, root *html.Node, limit int) []RawArticle {
	var items []RawArticle
	for _, script := range selectNodes(root, "script[type='application/ld+json']") {
		body := strings.TrimSpace(nodeText(script))
		if body == "" {
			continue
		}
		var payload any
		if err := json.Unmarshal([]byte(body), &payload); err != nil {
			continue
		}
		for _, record := range flattenJSONLDPayload(payload) {
			article := buildRawFromJSONLDRecord(profile, record)
			if article != nil {
				items = append(items, *article)
				if len(items) >= limit {
					return items
				}
			}
		}
	}
	return items
}

func flattenJSONLDPayload(payload any) []map[string]any {
	var records []map[string]any
	switch v := payload.(type) {
	case map[string]any:
		if graph, ok := v["@graph"].([]any); ok {
			for _, entry := range graph {
				if m, ok := entry.(map[string]any); ok {
					records = append(records, m)
				}
			}
		}
		records = append(records, v)
	case []any:
		for _, entry := range v {
			if m, ok := entry.(map[string]any); ok {
				records = append(records, m)
			}
		}
	}
	return records
}

func buildRawFromJSONLDRecord(profile SourceProfile, record map[string]any) *RawArticle {
	schemaType := strings.ToLower(fmt.Sprint(record["@type"]))
	if !strings.Contains(schemaType, "article") &&
		!strings.Contains(schemaType, "scholarlyarticle") &&
		!strings.Contains(schemaType, "creativework") {
		return nil
	}
	title := strings.TrimSpace(stringValue(record, "headline", "name"))
	urlText := strings.TrimSpace(stringValue(record, "url", "mainEntityOfPage"))
	abstract := strings.TrimSpace(stringValue(record, "description"))
	journal := ""
	if part, ok := record["isPartOf"].(map[string]any); ok {
		journal = strings.TrimSpace(stringValue(part, "name", "headline"))
	} else if v, ok := record["isPartOf"]; ok {
		journal = strings.TrimSpace(fmt.Sprint(v))
	}
	datePublished := fmt.Sprint(record["datePublished"])
	doi := ExtractDOI(strings.Join([]string{
		title, abstract, fmt.Sprint(record["identifier"]), fmt.Sprint(record["sameAs"]),
	}, " "))
	year := ExtractYear(datePublished)
	if year == 0 {
		year = ExtractYear(title + " " + abstract)
	}
	if title == "" || urlText == "" {
		return nil
	}
	raw := buildRaw(profile, title, urlText, abstract,
		strings.Join([]string{title, abstract, journal}, " "),
		doi, journal, year, nil, "", "", "", "")
	if journal == "" {
		raw.Journal = strings.ToUpper(profile.SourceKey)
	}
	return &raw
}

func stringValue(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok && s != "" {
				return s
			}
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// RawArticle construction (parity with BaseConnector._raw).
// ---------------------------------------------------------------------------

// buildRaw normalizes every field exactly as _raw does and falls back to the
// profile language default.
func buildRaw(profile SourceProfile, title, urlText, abstract, fullText, doi, journal string, year int, authors []string, volume, issue, pages, language string) RawArticle {
	if language == "" {
		language = profile.Language
	}
	var cleanAuthors []string
	for _, a := range authors {
		if t := strings.TrimSpace(a); t != "" {
			cleanAuthors = append(cleanAuthors, t)
		}
	}
	return RawArticle{
		SourceKey: profile.SourceKey,
		Title:     NormalizeScholarly(title, 900),
		URL:       urlText,
		Abstract:  NormalizeScholarly(abstract, 8000),
		FullText:  NormalizeScholarly(fullText, -1),
		Language:  language,
		Year:      intPtrOrNil(year),
		DOI:       NormalizeScholarly(doi, 128),
		Journal:   NormalizeScholarly(journal, 300),
		Authors:   cleanAuthors,
		Volume:    NormalizeScholarly(volume, 32),
		Issue:     NormalizeScholarly(issue, 32),
		Pages:     NormalizeScholarly(pages, 32),
	}
}

func intPtrOrNil(v int) *int {
	if v <= 0 {
		return nil
	}
	return &v
}

// isConnectorError reports whether err is one of the connector error types.
func isConnectorError(err error) bool {
	var fe *FetchError
	var re *RetryableError
	var ce *ChallengeError
	return errors.As(err, &fe) || errors.As(err, &re) || errors.As(err, &ce)
}

// ---------------------------------------------------------------------------
// Landing-page enrichment (parity with BaseConnector.enrich_raw).
// ---------------------------------------------------------------------------

// enrichRawBase enriches a raw article with metadata parsed from its landing
// page. Returns (nil, nil) when the page is not a real article (sources that
// set _NON_ARTICLE_LANDING_META) and the record must be dropped. On transport
// failures it returns the original raw unchanged, mirroring the Django
// degrade-not-fail behavior; challenge pages are raised as errors.
func (e *HTMLEngine) enrichRawBase(ctx context.Context, raw *RawArticle, nonArticleMeta []string) (*RawArticle, error) {
	bt := e.Browser
	if !strings.HasPrefix(raw.URL, "http") {
		return raw, nil
	}
	htmlBody, err := requestText(ctx, bt, raw.SourceKey, raw.URL, nil, "", 0, ocrLanguage(raw.Language))
	if err != nil {
		if isConnectorError(err) {
			// Django's base enrich_raw degrades on transport errors but lets
			// ConnectorFetchError (challenge pages) propagate.
			return nil, err
		}
		return raw, nil
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return raw, nil //nolint:nilerr // Django parses leniently; degrade rather than fail the record
	}
	sanitizeHTML(root)
	metaText := NormalizeScholarly(extractMetaText(root), 12000)
	bodyText := htmlText(root)
	pageSample := bodyText
	if runes := []rune(pageSample); len(runes) > 20000 {
		pageSample = string(runes[:20000])
	}
	combinedPageText := metaText + " " + pageSample
	if IsChallengePage(combinedPageText) {
		return nil, fetchErr(raw.SourceKey, "challenge page returned for article landing page")
	}
	if len(nonArticleMeta) > 0 {
		if extractMetaContent(root, nonArticleMeta) == "" {
			//nolint:nilnil // Django parity: issue/TOC landing pages drop the record
			return nil, nil
		}
	}
	pdfURL := extractPDFURL(root, raw.URL, combinedPageText)
	pdfText := ""
	if pdfURL != "" && pdfURL != raw.URL {
		pdfText, err = requestPDFText(ctx, bt, raw.SourceKey, pdfURL, ocrLanguage(raw.Language))
		if err != nil {
			pdfText = ""
		}
	}
	doi := raw.DOI
	if doi == "" {
		doi = ExtractDOI(combinedPageText + " " + pdfText)
	}
	year := 0
	if raw.Year != nil {
		year = *raw.Year
	}
	if year == 0 {
		year = ExtractYear(combinedPageText + " " + pdfText)
	}
	journal := raw.Journal
	if strings.EqualFold(strings.TrimSpace(journal), raw.SourceKey) {
		if j := extractMetaContent(root, []string{
			"citation_journal_title", "dc.source", "dc.Source",
			"prism.publicationname", "og:site_name",
		}); j != "" {
			journal = j
		}
	}
	abstract := raw.Abstract
	if abstract == "" {
		abstract = extractMetaContent(root, []string{
			"citation_abstract", "description", "dc.description", "og:description",
		})
	}
	peer, indexing, preprint := MergeEvidence(combinedPageText, raw.PeerReviewEvidence, raw.IndexingEvidence, raw.PreprintEvidence)
	preprint = mergeTokenSet(strings.ToLower(combinedPageText+" "+pdfText), preprint, preprintTokens)
	sourceText := pdfText
	if sourceText == "" {
		sourceText = bodyText
	}
	fullText := raw.FullText + " " + pageSample
	if sourceText != "" {
		fullText = raw.Title + " " + sourceText
	}
	if e.Resolver != nil && (sourceText != "" || raw.DOI != "") {
		if resolved := e.Resolver.Resolve(ctx, raw, sourceText); resolved != "" {
			fullText = raw.Title + " " + resolved
		}
	}
	out := *raw
	out.DOI = doi
	out.Year = intPtrOrNil(year)
	out.Journal = NormalizeScholarly(journal, 300)
	out.Abstract = NormalizeScholarly(abstract, 8000)
	out.FullText = NormalizeScholarly(fullText, -1)
	out.PeerReviewEvidence = sliceLen(peer, 3000)
	out.IndexingEvidence = sliceLen(indexing, 3000)
	out.PreprintEvidence = sliceLen(preprint, 3000)
	return &out, nil
}

func sliceLen(s string, max int) string {
	if runes := []rune(s); len(runes) > max {
		return strings.TrimRight(string(runes[:max]), " \t\n")
	}
	return s
}
