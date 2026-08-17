package connector

import (
	"context"
	"regexp"
	"strings"

	"golang.org/x/net/html"
)

// MathNet.Ru: parity with MathNetConnector in html_connectors.py — POST search
// with multiple attempt queries, relevance-preferring result parsing, and a
// structural page enrichment (citation head from <title>, bibliographic line
// from the first matching <i>, labeled <b> fields).

const mathNetBase = "https://www.mathnet.ru"

// minTitleLength is the minimum article title length shared by the HTML
// connectors (parity with _MIN_TITLE_LENGTH in html_connectors.py).
const minTitleLength = 14

// mathnetCitationHeadRe parses `Authors, "Title", …` from the <title> tag.
var mathnetCitationHeadRe = regexp.MustCompile(`\s*([^“”"]+?)\s*,\s*[“”"]([^“”"]+)[””"]`)

// mathnetJournalRe anchors the journal name before the year/Forthcoming marker.
var mathnetJournalRe = regexp.MustCompile(`\s*(.+?)\s*,\s*(?:\d{4}|Forthcoming)`)

var (
	mathnetYearRe   = regexp.MustCompile(`,\s*(\d{4})\s*,`)
	mathnetVolRe    = regexp.MustCompile(`Volume\s+(\d+)`)
	mathnetIssueRe  = regexp.MustCompile(`Issue\s+(\d+(?:\(\d+\))?)`)
	mathnetPagesRe  = regexp.MustCompile(`Pages\s+(\d+(?:[–-]\d+)?)`)
	mathnetDOIRe    = regexp.MustCompile(`(?i)doi\.org/(10\.\S+?)(?:\s|$)`)
	mathnetAuthorRe = regexp.MustCompile(`\s+and\s+|,`)
)

// mathnetLanguageMap maps the MathNet Language: label to an ISO 639-1 code.
var mathnetLanguageMap = map[string]string{
	"english": "en", "russian": "ru", "french": "fr", "german": "de",
}

func (e *HTMLEngine) fetchMathNet(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	attempts := []string{strings.TrimSpace(query), strings.TrimSpace(query)}
	tokens := queryTokens(query)
	if len(tokens) > 0 {
		attempts = append(attempts, tokens[0])
	}
	attempts = append(attempts, "probability", "probability")
	for _, attemptQuery := range attempts {
		if attemptQuery == "" {
			continue
		}
		items, err := e.searchMathNet(ctx, attemptQuery, limit)
		if err != nil {
			if isConnectorError(err) {
				continue
			}
			return nil, err
		}
		if len(items) > 0 {
			return items, nil
		}
	}
	return e.fetchMathNetHomeFallback(ctx, limit)
}

// searchMathNet posts one search query and parses the result links
// (parity with _search_mathnet: relevant /eng/ links preferred, candidates
// bounded at limit*5).
func (e *HTMLEngine) searchMathNet(ctx context.Context, searchQuery string, limit int) ([]RawArticle, error) {
	htmlBody, err := e.postMathNetSearch(ctx, searchQuery)
	if err != nil {
		return nil, err
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return nil, fetchErr("mathnet", "invalid HTML: %v", err)
	}
	var candidates []RawArticle
	var relevant []RawArticle
	for _, link := range selectNodes(root, "a[href*='/eng/']") {
		article, isRelevant := parseMathNetLink(link)
		if article == nil {
			continue
		}
		candidates = append(candidates, *article)
		if isRelevant {
			relevant = append(relevant, *article)
		}
		if len(candidates) >= limit*5 {
			break
		}
	}
	if len(relevant) > 0 {
		return relevant[:minInt(limit, len(relevant))], nil
	}
	if len(candidates) > 0 {
		return candidates[:minInt(limit, len(candidates))], nil
	}
	return nil, nil
}

func (e *HTMLEngine) postMathNetSearch(ctx context.Context, searchQuery string) (string, error) {
	pageResult, err := e.Browser.PostForm(ctx, "mathnet",
		mathNetBase+"/php/searchpapers_do.phtml?jrnid=&option_lang=eng", map[string]string{
			"tjrnid":       "",
			"keywords":     searchQuery,
			"where_keyw":   "any",
			"authors":      "",
			"organisation": "",
			"fundername":   "",
			"grantnumber":  "",
			"v1":           "",
			"v2":           "",
			"yr1":          "",
			"yr2":          "",
		}, "")
	if err != nil {
		return "", err
	}
	return pageResult.Body, nil
}

// parseMathNetLink builds a raw article from one search-result link, using
// the enclosing <tr> (when present) as context (parity with
// _parse_mathnet_link).
func parseMathNetLink(link *html.Node) (*RawArticle, bool) {
	title := strings.TrimSpace(nodeText(link))
	href := resolveURL(mathNetBase, nodeAttr(link, "href"))
	contextText := nodeText(link)
	if tr := findAncestor(link, "tr"); tr != nil {
		contextText = nodeText(tr)
	}
	combined := title + " " + contextText
	doi := ExtractDOI(combined)
	year := ExtractYear(combined)
	if utf8Len(title) < minTitleLength || !strings.HasPrefix(href, "http") {
		return nil, false
	}
	raw := buildRaw(mustProfile("mathnet"), title, href, sliceLen(contextText, 700),
		combined, doi, "MathNet.Ru", year, nil, "", "", "", "")
	isRelevant := strings.Contains(href, "/eng/")
	return &raw, isRelevant
}

// findAncestor returns the nearest ancestor element with the given tag name.
func findAncestor(n *html.Node, tag string) *html.Node {
	for p := n.Parent; p != nil; p = p.Parent {
		if p.Type == html.ElementNode && p.Data == tag {
			return p
		}
	}
	return nil
}

// utf8Len counts runes (Django len() counts code points).
func utf8Len(s string) int {
	return len([]rune(s))
}

// enrichMathNet parses the citation head, bibliographic <i> line and labeled
// fields from the article landing page (parity with _mathnet_enrich_raw).
func (e *HTMLEngine) enrichMathNet(ctx context.Context, raw *RawArticle) (*RawArticle, error) {
	enriched, err := e.enrichRawBase(ctx, raw, nil)
	if err != nil {
		return nil, err
	}
	if enriched == nil {
		//nolint:nilnil // Django parity: non-article landing pages drop the record
		return nil, nil
	}
	if !strings.HasPrefix(raw.URL, "http") {
		return enriched, nil
	}
	htmlBody, err := requestText(ctx, e.Browser, "mathnet", raw.URL, nil, "", 0, ocrLanguage(raw.Language))
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
	pageText := htmlText(root)
	if IsChallengePage(pageText) {
		return enriched, nil
	}

	titleText := ""
	if titles := selectNodes(root, "title"); len(titles) > 0 {
		titleText = strings.TrimSpace(nodeText(titles[0]))
	}
	authorsBlob, parsedTitle := mathnetCitationHead(titleText)
	journal, volume, issue, pages, yearStr, doi := mathnetItalicsMeta(root)
	abstract := mathnetLabeledValue(root, "Abstract:")
	language := mathnetLanguageCode(mathnetLabeledValue(root, "Language:"))

	finalTitle := parsedTitle
	if finalTitle == "" {
		finalTitle = enriched.Title
	}
	finalJournal := journal
	if finalJournal == "" {
		finalJournal = enriched.Journal
	}
	authors := enriched.Authors
	if authorsBlob != "" {
		authors = splitMathNetAuthors(authorsBlob)
	}
	finalYear := enriched.Year
	if yearStr != "" {
		finalYear = intPtrOrNil(ToInt(yearStr))
	}
	finalDOI := doi
	if finalDOI == "" {
		finalDOI = enriched.DOI
	}
	if finalDOI == "" {
		finalDOI = ExtractDOI(pageText)
	}

	volIssue := ""
	if volume != "" || issue != "" {
		volIssue = volume + ":" + issue
	}
	var parts []string
	for _, part := range []string{finalTitle, authorsBlob, finalJournal, volIssue, pages, abstract, pageText} {
		if part != "" {
			parts = append(parts, part)
		}
	}
	fullText := sliceLen(strings.Join(parts, " "), 20000)

	out := *enriched
	out.Title = NormalizeScholarly(finalTitle, 900)
	if abstract != "" {
		out.Abstract = NormalizeScholarly(abstract, 8000)
	}
	out.FullText = NormalizeScholarly(fullText, -1)
	out.DOI = finalDOI
	out.Year = finalYear
	out.Journal = NormalizeScholarly(finalJournal, 300)
	out.Authors = authors
	out.Volume = NormalizeScholarly(volume, 32)
	out.Issue = NormalizeScholarly(issue, 32)
	out.Pages = NormalizeScholarly(pages, 32)
	if language != "" {
		out.Language = language
	}
	return &out, nil
}

// mathnetCitationHead parses `Authors, "Title", …` from the <title> tag.
func mathnetCitationHead(titleText string) (string, string) {
	m := mathnetCitationHeadRe.FindStringSubmatch(titleText)
	if m == nil {
		return "", ""
	}
	return strings.TrimSpace(m[1]), strings.TrimSpace(m[2])
}

// mathnetItalicsMeta parses the bibliographic line from the first <i> element
// carrying the DOI/Forthcoming/Volume markers.
func mathnetItalicsMeta(root *html.Node) (journal, volume, issue, pages, year, doi string) {
	var line string
	walkHTML(root, func(n *html.Node) bool {
		if n.Type == html.ElementNode && n.Data == "i" {
			text := strings.TrimSpace(nodeText(n))
			if strings.Contains(text, "DOI:") || strings.Contains(text, "Forthcoming") || strings.Contains(text, "Volume") {
				line = text
				return true
			}
		}
		return false
	})
	if line == "" {
		return "", "", "", "", "", ""
	}
	if m := mathnetJournalRe.FindStringSubmatch(line); m != nil {
		journal = strings.TrimSpace(m[1])
	} else if parts := strings.SplitN(line, ",", 2); len(parts) > 0 {
		journal = strings.TrimSpace(parts[0])
	}
	journal = sliceLen(journal, 300)
	if m := mathnetYearRe.FindStringSubmatch(line); m != nil {
		year = m[1]
	}
	if m := mathnetVolRe.FindStringSubmatch(line); m != nil {
		volume = m[1]
	}
	if m := mathnetIssueRe.FindStringSubmatch(line); m != nil {
		issue = m[1]
	}
	if m := mathnetPagesRe.FindStringSubmatch(line); m != nil {
		pages = m[1]
	}
	if m := mathnetDOIRe.FindStringSubmatch(line); m != nil {
		doi = strings.TrimRight(m[1], ".,;)")
	}
	return journal, volume, issue, pages, year, doi
}

// mathnetLabeledValue returns the text following a <b>Label:</b> element,
// walking siblings until the next <b> (parity with _mathnet_labeled_value).
func mathnetLabeledValue(root *html.Node, label string) string {
	var value string
	walkHTML(root, func(n *html.Node) bool {
		if n.Type != html.ElementNode || n.Data != "b" {
			return false
		}
		if strings.TrimSpace(nodeText(n)) != label {
			return false
		}
		var parts []string
		for sibling := n.NextSibling; sibling != nil; sibling = sibling.NextSibling {
			switch {
			case sibling.Type == html.ElementNode && sibling.Data == "b":
				goto done
			case sibling.Type == html.ElementNode && sibling.Data == "br":
				parts = append(parts, " ")
			case sibling.Type == html.TextNode:
				parts = append(parts, sibling.Data)
			case sibling.Type == html.ElementNode:
				parts = append(parts, nodeText(sibling))
			}
		}
	done:
		v := strings.TrimSpace(normalizeSpaces(strings.Join(parts, " ")))
		if v != "" {
			value = v
			return true
		}
		return false
	})
	return value
}

func normalizeSpaces(s string) string {
	return regexp.MustCompile(`\s+`).ReplaceAllString(s, " ")
}

// splitMathNetAuthors splits an author blob on " and " / commas, strips
// separators and de-duplicates while preserving order.
func splitMathNetAuthors(authorBlob string) []string {
	cleaned := strings.TrimSpace(strings.TrimSpace(normalizeSpaces(authorBlob)))
	cleaned = strings.TrimPrefix(cleaned, ",")
	cleaned = strings.TrimSuffix(cleaned, ",")
	if cleaned == "" {
		return nil
	}
	var parts []string
	for _, part := range mathnetAuthorRe.Split(cleaned, -1) {
		if t := strings.Trim(part, " ,;"); t != "" {
			parts = append(parts, t)
		}
	}
	seen := make(map[string]bool, len(parts))
	var out []string
	for _, p := range parts {
		if !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	return out
}

// mathnetLanguageCode maps the MathNet Language: label to ISO 639-1.
func mathnetLanguageCode(label string) string {
	return mathnetLanguageMap[strings.ToLower(strings.TrimSpace(label))]
}

// fetchMathNetHomeFallback lists /eng/ links from the plain search page when
// every attempt query fails (parity with _fetch_home_fallback).
func (e *HTMLEngine) fetchMathNetHomeFallback(ctx context.Context, limit int) ([]RawArticle, error) {
	htmlBody, err := requestText(ctx, e.Browser, "mathnet",
		mathNetBase+"/php/search.phtml?wshow=search&option_lang=eng", nil, "", 0, ocrLanguage("ru"))
	if err != nil {
		if isConnectorError(err) {
			//nolint:nilerr // Django parity: connector failures yield an empty result set
			return nil, nil
		}
		return nil, err
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return nil, nil //nolint:nilerr // Django parses leniently; an unparsable page yields no results
	}
	items := make([]RawArticle, 0, limit)
	for _, link := range selectNodes(root, "a[href*='/eng/']") {
		title := strings.TrimSpace(nodeText(link))
		href := resolveURL(mathNetBase, nodeAttr(link, "href"))
		if utf8Len(title) < minTitleLength || !strings.HasPrefix(href, "http") {
			continue
		}
		parentText := ""
		if link.Parent != nil {
			parentText = nodeText(link.Parent)
		}
		combined := title + " " + parentText
		items = append(items, buildRaw(mustProfile("mathnet"), title, href,
			sliceLen(combined, 700), combined, ExtractDOI(combined), "MathNet.Ru",
			ExtractYear(combined), nil, "", "", "", ""))
		if len(items) >= limit {
			break
		}
	}
	return items, nil
}

// queryTokens returns the lower-cased whitespace tokens longer than 2 chars
// (parity with SciELOConnector._query_terms, reused by MathNet).
func queryTokens(query string) []string {
	var tokens []string
	for _, m := range wordTokens.FindAllString(strings.ToLower(query), -1) {
		if len(m) > 2 {
			tokens = append(tokens, m)
		}
	}
	return tokens
}
