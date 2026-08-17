package connector

import (
	"context"
	"fmt"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"golang.org/x/net/html"
)

// SciELO: parity with SciELOConnector in html_connectors.py — RSS keyword
// search first (with throttling backoff), then the date-based OAI harvest with
// client-side term filtering, then the HTML search; enrichment runs through
// the ArticleMeta REST API (the landing page sits behind a CDN interstitial
// the sidecar cannot reliably clear).

const (
	scieloRSSURL           = "https://search.scielo.org/"
	scieloArticleMetaAPI   = "https://articlemeta.scielo.org/api/v1/article/"
	scieloOAIResumptionMax = 8
	scieloMinQueryTermLen  = 3
)

var scieloOAMirrors = []string{
	"https://scielo.isciii.es/oai/scielo-oai.php",
	"https://www.scielo.org.mx/oai/scielo-oai.php",
}

// scieloRSSHeaderRe matches the `Resumo em <lang> <Resumo|Abstract|...>` label
// that precedes the real abstract text.
var scieloRSSHeaderRe = regexp.MustCompile(`(?i)Resumo em \w+\s+(?:Resum[oa]s?|Resumen|Abstract|RESUM[OA]S?|RESUMEN|ABSTRACT)\b`)

// scieloRSSMarkerRe matches the language-specific abstract header word alone.
var scieloRSSMarkerRe = regexp.MustCompile(`(?i)(?:Resum[oa]s?|Resumen|Abstract|RESUM[OA]S?|RESUMEN|ABSTRACT)\b`)

// scieloOAIJournalTailRe strips the volume/issue/year tail from dc:source.
var scieloOAIJournalTailRe = regexp.MustCompile(`(?i)\s+(?:v|vol|n|no|nº|num)\.?\s*\d.*$`)

var (
	scieloResourcePIDRe = regexp.MustCompile(`/resource/[a-z]+/(S[A-Za-z0-9-]+)(?:[/?#]|$)`)
	scieloQueryPIDRe    = regexp.MustCompile(`[?&]pid=(S[A-Za-z0-9-]+)`)
	scieloCollectionRe  = regexp.MustCompile(`^(S.*)-([a-z]{2,5})$`)
	scieloAbstractLabel = regexp.MustCompile(`(?i)^(?:RESUM[EO]S?|RESUMEN(?:ES)?|ABSTRACTS?|SUMMAR(?:Y|IES)|ZUSAMMENFASSUNG(?:EN)?|RIASSUNT[OI]|SAMENVATTING(?:EN)?|RÉSUMÉS?)\s+`)
)

func (e *HTMLEngine) fetchSciELO(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	items, err := e.fetchSciELORSS(ctx, query, limit)
	if err == nil && len(items) > 0 {
		return items, nil
	}
	if items, err = e.fetchSciELOOAI(ctx, query, limit); err == nil && len(items) > 0 {
		return items, nil
	}
	return e.fetchSciELOHTML(ctx, query, limit)
}

// fetchSciELORSS fetches the RSS keyword feed, retrying with the Django
// throttle backoff (8s, 16s, 24s).
func (e *HTMLEngine) fetchSciELORSS(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	params := map[string]string{"q": query, "count": strconv.Itoa(maxInt(limit, 5)), "output": "rss"}
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		text, err := requestText(ctx, e.Browser, "scielo", scieloRSSURL, params, "", 0, ocrLanguage("es"))
		if err == nil {
			return parseSciELORSS(text, query, limit), nil
		}
		lastErr = err
		select {
		case <-time.After(time.Duration(8*(attempt+1)) * time.Second):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return nil, fetchErr("scielo", "rss search throttled: %v", lastErr)
}

// parseSciELORSS parses the RSS feed; titles are multilingual
// `primary / secondary / tertiary` strings and the first segment is kept.
func parseSciELORSS(xmlText, query string, limit int) []RawArticle {
	items, err := parseFeed(xmlText)
	if err != nil {
		return nil
	}
	profile := mustProfile("scielo")
	var out []RawArticle
	for _, item := range items {
		titleRaw := strings.TrimSpace(item.Title)
		title := ""
		if titleRaw != "" {
			title = strings.TrimSpace(strings.Split(titleRaw, " / ")[0])
		}
		if title == "" {
			continue
		}
		urlValue := strings.TrimSpace(item.Link)
		if urlValue == "" {
			continue
		}
		authorBlob := strings.Join(item.Creators, " ")
		authors := splitSciELORSSAuthors(authorBlob)
		abstract := cleanSciELORSSAbstract(item.Description)
		year := ExtractYear(urlValue)
		if year == 0 {
			year = ExtractYear(item.Description)
		}
		doi := ExtractDOI(item.Description)
		if doi == "" {
			doi = ExtractDOI(urlValue)
		}
		journal := "SciELO"
		combined := strings.Join([]string{title, abstract, authorBlob, journal, urlValue, item.Description}, " ")
		if !IsArticleLikeItem(title, urlValue, doi, year) {
			continue
		}
		out = append(out, buildRaw(profile, title, urlValue, abstract, combined,
			doi, journal, year, authors, "", "", "", ""))
		if len(out) >= limit {
			break
		}
	}
	return out
}

// splitSciELORSSAuthors splits on ";" preserving the "Last, First" comma
// inside each name; empty entries and exact duplicates are dropped.
func splitSciELORSSAuthors(authorBlob string) []string {
	cleaned := strings.TrimSpace(normalizeSpaces(authorBlob))
	cleaned = strings.TrimPrefix(cleaned, ";")
	cleaned = strings.TrimSuffix(cleaned, ";")
	if cleaned == "" {
		return nil
	}
	var parts []string
	for _, part := range strings.Split(cleaned, ";") {
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

// cleanSciELORSSAbstract strips the leading author list and language label
// from an RSS description (parity with _clean_rss_abstract).
func cleanSciELORSSAbstract(desc string) string {
	desc = strings.TrimSpace(desc)
	if desc == "" {
		return ""
	}
	if m := scieloRSSHeaderRe.FindString(desc); m != "" {
		return sliceLen(strings.TrimSpace(desc[len(m):]), 1500)
	}
	if m := scieloRSSMarkerRe.FindString(desc); m != "" {
		return sliceLen(strings.TrimSpace(desc[len(m):]), 1500)
	}
	return ""
}

// fetchSciELOOAI harvests OAI mirrors with resumption tokens, filtering
// records client-side against the query terms (AND semantics).
func (e *HTMLEngine) fetchSciELOOAI(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	terms := queryTokens(query)
	fromDate := maxInt(2000, time.Now().UTC().Year()-8)
	for _, endpoint := range scieloOAMirrors {
		u := endpoint + "?verb=ListRecords&metadataPrefix=oai_dc&from=" + strconv.Itoa(fromDate) + "-01-01"
		var items []RawArticle
		for page := 0; page < scieloOAIResumptionMax; page++ {
			xmlText, err := e.requestXMLText(ctx, u)
			if err != nil {
				if isConnectorError(err) {
					break // move to the next mirror
				}
				return nil, err
			}
			records, token, err := parseOAI(xmlText)
			if err != nil {
				break
			}
			for _, rec := range records {
				article := parseSciELOOAIRecord(rec)
				if article == nil {
					continue
				}
				if len(terms) > 0 && !articleMatchesTerms(article, terms) {
					continue
				}
				items = append(items, *article)
				if len(items) >= limit {
					return items[:limit], nil
				}
			}
			if token == "" {
				break
			}
			u = endpoint + "?verb=ListRecords&resumptionToken=" + quotePlus(token)
		}
		if len(items) > 0 {
			return items[:minInt(limit, len(items))], nil
		}
	}
	return nil, fetchErr("scielo", "oai mirrors yielded no query-relevant entries")
}

// requestXMLText fetches XML through the sidecar (parity with
// _request_xml_text: transport failures surface so the mirror loop moves on).
func (e *HTMLEngine) requestXMLText(ctx context.Context, u string) (string, error) {
	page, err := e.Browser.Fetch(ctx, "scielo", u, nil, "application/xml,text/xml,*/*", 0)
	if err != nil {
		return "", err
	}
	if IsChallengePage(page.Body) {
		return "", fetchErr("scielo", "challenge page unresolved")
	}
	return page.Body, nil
}

// parseSciELOOAIRecord builds a raw article from one OAI record; nil for
// deleted records, missing titles, or non-article-like items.
func parseSciELOOAIRecord(rec oaiRecord) *RawArticle {
	if rec.Deleted {
		return nil
	}
	titles := rec.Fields["title"]
	if len(titles) == 0 {
		return nil
	}
	title := strings.TrimSpace(titles[0])
	urlValue := ""
	for _, id := range rec.Fields["identifier"] {
		if strings.HasPrefix(id, "http") {
			urlValue = id
			break
		}
	}
	abstract := ""
	if descs := rec.Fields["description"]; len(descs) > 0 {
		abstract = strings.TrimSpace(descs[0])
	}
	journal := ""
	if sources := rec.Fields["source"]; len(sources) > 0 {
		journal = cleanSciELOOAIJournal(sources[0])
	}
	if journal == "" {
		journal = "SciELO"
	}
	dates := rec.Fields["date"]
	subjects := rec.Fields["subject"]
	ids := rec.Fields["identifier"]
	combined := strings.Join([]string{title, abstract, journal, strings.Join(subjects, " "), strings.Join(ids, " ")}, " ")
	doi := ExtractDOI(combined)
	year := ExtractYear(strings.Join(append(dates, title), " "))
	if urlValue == "" {
		return nil
	}
	if !IsArticleLikeItem(title, urlValue, doi, year) {
		return nil
	}
	raw := buildRaw(mustProfile("scielo"), title, urlValue, abstract, combined,
		doi, journal, year, nil, "", "", "", "")
	return &raw
}

// cleanSciELOOAIJournal truncates the volume/issue/year tail from dc:source.
func cleanSciELOOAIJournal(raw string) string {
	return strings.TrimSpace(scieloOAIJournalTailRe.ReplaceAllString(raw, ""))
}

// articleMatchesTerms reports whether every query term appears in the
// article's text fields (AND semantics, parity with _article_matches_terms).
func articleMatchesTerms(article *RawArticle, terms []string) bool {
	haystack := strings.ToLower(strings.Join([]string{
		article.Title, article.Abstract, article.FullText, article.Journal,
	}, " "))
	for _, term := range terms {
		if !strings.Contains(haystack, term) {
			return false
		}
	}
	return true
}

// fetchSciELOHTML tries the profile search page then the search.scielo.org
// summary endpoint (parity with _fetch_html + _extract_from_html).
func (e *HTMLEngine) fetchSciELOHTML(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	profile := mustProfile("scielo")
	attempts := []struct {
		url    string
		params map[string]string
	}{
		{profile.SearchURL, map[string]string{"q": query}},
		{scieloRSSURL, map[string]string{"q": query, "lang": "en", "count": "20", "from": "0", "output": "site", "format": "summary", "page": "1"}},
	}
	for _, attempt := range attempts {
		htmlBody, err := requestText(ctx, e.Browser, "scielo", attempt.url, attempt.params, "", 0, ocrLanguage(profile.Language))
		if err != nil {
			if isConnectorError(err) {
				continue
			}
			return nil, err
		}
		root, err := parseHTMLBody(htmlBody)
		if err != nil {
			continue
		}
		if err := assertPageParseable("scielo", htmlBody, root); err != nil {
			continue
		}
		items := extractSciELOFromHTML(profile, root, limit)
		if len(items) > 0 {
			return items, nil
		}
	}
	return nil, fetchErr("scielo", "unable to obtain parseable result page")
}

func extractSciELOFromHTML(profile SourceProfile, root *html.Node, limit int) []RawArticle {
	rows := selectNodes(root, ".item, .search-results .item, .result, article, li")
	candidates := make([]RawArticle, 0, limit)
	for _, row := range rows {
		titleNodes := selectNodes(row, ".title a, h2 a, h3 a, a[href]")
		if len(titleNodes) == 0 {
			continue
		}
		title := strings.TrimSpace(nodeText(titleNodes[0]))
		href := resolveURL(profile.SearchURL, nodeAttr(titleNodes[0], "href"))
		abstract := ""
		if absNodes := selectNodes(row, ".abstract, .snippet, .description, p"); len(absNodes) > 0 {
			abstract = strings.TrimSpace(nodeText(absNodes[0]))
		}
		journal := strings.ToUpper(profile.SourceKey)
		if jNodes := selectNodes(row, ".journal, .publication, .source, .meta"); len(jNodes) > 0 {
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
		candidates = append(candidates, buildRaw(profile, title, href, abstract, combined,
			doi, journal, year, nil, "", "", "", ""))
		if len(candidates) >= limit*3 {
			break
		}
	}
	if len(candidates) > 0 {
		return candidates[:minInt(limit, len(candidates))]
	}
	return extractJSONLDArticles(profile, root, limit)
}

// enrichSciELO enriches via the ArticleMeta REST API (parity with
// SciELOConnector.enrich_raw: any ArticleMeta failure returns the raw record
// unchanged).
func (e *HTMLEngine) enrichSciELO(ctx context.Context, raw *RawArticle) (*RawArticle, error) {
	if !strings.HasPrefix(raw.URL, "http") {
		return raw, nil
	}
	code, collection := scieloPIDFromURL(raw.URL)
	if code == "" {
		return raw, nil
	}
	params := url.Values{}
	params.Set("code", code)
	if collection != "" {
		params.Set("collection", collection)
	}
	var data map[string]any
	if err := e.Direct.GetJSON(ctx, "scielo", scieloArticleMetaAPI+"?"+params.Encode(), nil, &data); err != nil {
		if isConnectorError(err) {
			return raw, nil // Django parity: ConnectorFetchError degrades to the enriched record
		}
		return raw, err
	}
	if data == nil {
		return raw, nil
	}
	journal := scieloArticleMetaJournal(data)
	if journal == "" {
		journal = raw.Journal
	}
	doi := scieloArticleMetaDOI(data)
	if doi == "" {
		doi = raw.DOI
	}
	year := raw.Year
	if y := scieloArticleMetaYear(data); y != nil {
		year = y
	}
	abstract := scieloArticleMetaAbstract(data)
	if abstract == "" {
		abstract = raw.Abstract
	}
	authors := scieloArticleMetaAuthors(data)
	if len(authors) == 0 {
		authors = raw.Authors
	}
	combined := strings.Join([]string{raw.Title, abstract, journal, strings.Join(authors, " ")}, " ")
	peer, indexing, preprint := MergeEvidence(combined, raw.PeerReviewEvidence, raw.IndexingEvidence, raw.PreprintEvidence)
	fullText := NormalizeScholarly(strings.Join([]string{raw.Title, abstract, combined}, " "), -1)
	out := *raw
	out.DOI = doi
	out.Year = year
	out.Journal = NormalizeScholarly(journal, 300)
	out.Abstract = NormalizeScholarly(abstract, 8000)
	out.Authors = authors
	out.FullText = fullText
	out.PeerReviewEvidence = sliceLen(peer, 3000)
	out.IndexingEvidence = sliceLen(indexing, 3000)
	out.PreprintEvidence = sliceLen(preprint, 3000)
	return &out, nil
}

// scieloPIDFromURL extracts the PID code and optional collection from a
// SciELO URL (parity with _scielo_pid_from_url).
func scieloPIDFromURL(u string) (string, string) {
	if u == "" {
		return "", ""
	}
	if m := scieloResourcePIDRe.FindStringSubmatch(u); m != nil {
		pid := m[1]
		if c := scieloCollectionRe.FindStringSubmatch(pid); c != nil {
			return c[1], c[2]
		}
		return pid, ""
	}
	if m := scieloQueryPIDRe.FindStringSubmatch(u); m != nil {
		return m[1], ""
	}
	return "", ""
}

// scieloFirstField returns the "_" value of the first entry in an ISIS field
// list (parity with _first_field).
func scieloFirstField(block []any) string {
	if len(block) == 0 {
		return ""
	}
	first, ok := block[0].(map[string]any)
	if !ok {
		return ""
	}
	return strings.TrimSpace(fmtValue(first["_"]))
}

func fmtValue(v any) string {
	if v == nil {
		return ""
	}
	return strings.TrimSpace(strings.TrimPrefix(strings.TrimSuffix(strings.TrimSpace(fmt.Sprint(v)), "\n"), "\n"))
}

// scieloArticleMetaJournal returns the full journal name from title.v100.
func scieloArticleMetaJournal(data map[string]any) string {
	titleBlock, _ := data["title"].(map[string]any)
	if titleBlock == nil {
		return ""
	}
	v100, _ := titleBlock["v100"].([]any)
	return scieloFirstField(v100)
}

// scieloArticleMetaDOI returns the DOI from the top level or article.v237.
func scieloArticleMetaDOI(data map[string]any) string {
	if doi := strings.TrimSpace(fmtValue(data["doi"])); doi != "" {
		return doi
	}
	article, _ := data["article"].(map[string]any)
	if article == nil {
		return ""
	}
	v237, _ := article["v237"].([]any)
	return scieloFirstField(v237)
}

// scieloArticleMetaYear returns the publication year when it is digits-only.
func scieloArticleMetaYear(data map[string]any) *int {
	s := strings.TrimSpace(fmtValue(data["publication_year"]))
	if s == "" {
		return nil
	}
	for _, ch := range s {
		if ch < '0' || ch > '9' {
			return nil
		}
	}
	return intPtrOrNil(ToInt(s))
}

// scieloArticleMetaAbstract prefers the English v83 entry, then the article's
// original language (v40), then the first; the leading label is stripped.
func scieloArticleMetaAbstract(data map[string]any) string {
	article, _ := data["article"].(map[string]any)
	if article == nil {
		return ""
	}
	entries, _ := article["v83"].([]any)
	if len(entries) == 0 {
		return ""
	}
	original := strings.ToLower(scieloFirstField(scieloAnyList(article["v40"])))
	var chosen map[string]any
	for _, entry := range entries {
		if m, ok := entry.(map[string]any); ok && strings.ToLower(fmtValue(m["l"])) == "en" {
			chosen = m
			break
		}
	}
	if chosen == nil && original != "" {
		for _, entry := range entries {
			if m, ok := entry.(map[string]any); ok && strings.ToLower(fmtValue(m["l"])) == original {
				chosen = m
				break
			}
		}
	}
	if chosen == nil {
		chosen, _ = entries[0].(map[string]any)
	}
	if chosen == nil {
		return ""
	}
	text := strings.TrimSpace(fmtValue(chosen["a"]))
	text = strings.TrimSpace(scieloAbstractLabel.ReplaceAllString(text, ""))
	return sliceLen(text, 8000)
}

// scieloArticleMetaAuthors returns "surname, given" names from article.v10,
// dropping entries missing both fields and exact duplicates.
func scieloArticleMetaAuthors(data map[string]any) []string {
	article, _ := data["article"].(map[string]any)
	if article == nil {
		return nil
	}
	entries, _ := article["v10"].([]any)
	var authors []string
	for _, entry := range entries {
		m, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		surname := strings.TrimSpace(fmtValue(m["s"]))
		given := strings.TrimSpace(fmtValue(m["n"]))
		var name string
		switch {
		case surname != "" && given != "":
			name = surname + ", " + given
		case surname != "" || given != "":
			name = surname + given
		default:
			continue
		}
		authors = append(authors, name)
	}
	seen := make(map[string]bool, len(authors))
	var out []string
	for _, a := range authors {
		if !seen[a] {
			seen[a] = true
			out = append(out, a)
		}
	}
	return out
}

// scieloAnyList safely coerces a JSON field to a list.
func scieloAnyList(v any) []any {
	if l, ok := v.([]any); ok {
		return l
	}
	return nil
}
