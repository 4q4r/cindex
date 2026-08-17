package connector

import (
	"context"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/html"
)

// OAI-harvesting HTML-mode connectors: DergiPark, Hrčak and AJOL. Parity with
// html_connectors.py — OAI-PMH ListRecords harvests (date-based, no keyword
// search) post-filtered against the query tokens, plus the AJOL HTML search
// with its open-access filter.

// ---------------------------------------------------------------------------
// DergiPark (per-set OAI harvest; HTML search is Cloudflare-Turnstile-gated).
// ---------------------------------------------------------------------------

const dergiParkOAIBase = "https://dergipark.org.tr/api/public/oai/"

// dergiparkJournalTailRe strips the ", Vol. X" suffix from dc:source.
var dergiparkJournalTailRe = regexp.MustCompile(`(?i),\s*Vol\.`)

var (
	dergiparkSetsMu sync.Mutex
	dergiparkSets   []dergiparkSet
)

type dergiparkSet struct {
	spec string
	name string
}

func (e *HTMLEngine) fetchDergiPark(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	items, err := e.fetchDergiParkOAI(ctx, query, limit)
	if err != nil {
		if isConnectorError(err) {
			return fetchHTMLGeneric(ctx, e.Browser, mustProfile("dergipark"), query, limit)
		}
		return nil, err
	}
	return items, nil
}

// fetchDergiParkOAI harvests per-set ListRecords, skipping failing sets; if
// every set fails the harvest is treated as unreachable so the caller can
// fall back to HTML (parity with _fetch_via_oai).
func (e *HTMLEngine) fetchDergiParkOAI(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	sets, err := e.listDergiParkSets(ctx)
	if err != nil {
		return nil, err
	}
	if len(sets) == 0 {
		return nil, fetchErr("dergipark", "no OAI sets available")
	}
	maxSets := envInt("DERGIPARK_OAI_MAX_SETS", 60)
	recencyYear := time.Now().UTC().Year() - 2
	var results []RawArticle
	failures := 0
	for i, set := range sets {
		if i >= maxSets {
			break
		}
		if len(results) >= limit {
			break
		}
		u := dergiParkOAIBase + "?verb=ListRecords&metadataPrefix=oai_dc" +
			"&set=" + quotePlus(set.spec) + "&from=" + strconv.Itoa(recencyYear) + "-01-01"
		xmlText, err := requestText(ctx, e.Browser, "dergipark", u, nil, "", 0, ocrLanguage("en"))
		if err != nil {
			if isConnectorError(err) {
				failures++
				continue
			}
			return nil, err
		}
		parsed := parseDergiParkRecords(xmlText, query, set.name, limit-len(results))
		results = append(results, parsed...)
	}
	if len(results) > 0 {
		return results[:minInt(limit, len(results))], nil
	}
	if failures == minInt(maxSets, len(sets)) {
		return nil, fetchErr("dergipark", "all OAI sets cloudflare-gated")
	}
	return nil, nil
}

// listDergiParkSets lists the OAI sets with a package-level cache (parity
// with the _sets_cache class attribute).
func (e *HTMLEngine) listDergiParkSets(ctx context.Context) ([]dergiparkSet, error) {
	dergiparkSetsMu.Lock()
	defer dergiparkSetsMu.Unlock()
	if dergiparkSets != nil {
		return dergiparkSets, nil
	}
	xmlText, err := requestText(ctx, e.Browser, "dergipark", dergiParkOAIBase+"?verb=ListSets", nil, "", 0, ocrLanguage("en"))
	if err != nil {
		return nil, err
	}
	root, err := parseXMLRoot(xmlText)
	if err != nil {
		return nil, fetchErr("dergipark", "invalid OAI ListSets xml: %v", err)
	}
	if root.local() != "OAI-PMH" {
		return nil, fetchErr("dergipark", "invalid OAI ListSets response (root %q)", root.local())
	}
	var sets []dergiparkSet
	if list := root.child("ListSets"); list != nil {
		for _, setNode := range list.children("set") {
			spec := strings.TrimSpace(setNode.child("setSpec").text())
			if spec == "" {
				continue
			}
			name := strings.TrimSpace(setNode.child("setName").text())
			if name == "" {
				name = spec
			}
			sets = append(sets, dergiparkSet{spec: spec, name: name})
		}
	}
	dergiparkSets = sets
	return sets, nil
}

// parseDergiParkRecords parses one set's records, keeping only
// query-relevant ones (parity with _parse_oai_records + _build_oai_record).
func parseDergiParkRecords(xmlText, query, setName string, remaining int) []RawArticle {
	records, _, err := parseOAI(xmlText)
	if err != nil {
		return nil
	}
	var items []RawArticle
	for _, rec := range records {
		if rec.Deleted {
			continue
		}
		title := rec.first("title")
		if title == "" {
			continue
		}
		description := rec.first("description")
		subjects := rec.Fields["subject"]
		identifiers := rec.Fields["identifier"]
		var authors []string
		seenAuthors := make(map[string]bool)
		for _, name := range rec.Fields["creator"] {
			name = strings.TrimSpace(name)
			if name != "" && !seenAuthors[name] {
				authors = append(authors, name)
				seenAuthors[name] = true
			}
		}
		journal := dergiparkJournal(rec.Fields["source"], setName)
		language := strings.TrimSpace(rec.first("language"))
		// Relevance matching uses the topical fields only — NOT authors
		// (a query token coinciding with a surname would surface off-topic
		// articles).
		textBlob := strings.Join([]string{
			title, description, strings.Join(subjects, " "),
			strings.Join(identifiers, " "), journal,
		}, " ")
		if !MatchAllTerms(query, textBlob, "") {
			continue
		}
		doi := ExtractDOI(textBlob)
		dateValue := rec.first("date")
		year := ExtractYear(dateValue)
		if year == 0 {
			year = ExtractYear(textBlob)
		}
		urlValue := ""
		for _, ident := range identifiers {
			if strings.HasPrefix(ident, "http") {
				urlValue = ident
				break
			}
		}
		if urlValue == "" || !IsArticleLikeItem(title, urlValue, doi, year) {
			continue
		}
		items = append(items, buildRaw(mustProfile("dergipark"), title, urlValue,
			description, textBlob, doi, journal, year, authors, "", "", "", language))
		if len(items) >= remaining {
			break
		}
	}
	return items
}

// dergiparkJournal derives the journal title from dc:source entries, falling
// back to the OAI set name (sets are per-journal).
func dergiparkJournal(sources []string, setName string) string {
	for _, source := range sources {
		name := strings.TrimSpace(dergiparkJournalTailRe.Split(source, 2)[0])
		if name != "" {
			return name
		}
	}
	return setName
}

// ---------------------------------------------------------------------------
// Hrčak (OAI harvest with resumption tokens).
// ---------------------------------------------------------------------------

// hrcakLanguageMap maps ISO 639-3 codes to ISO 639-1.
var hrcakLanguageMap = map[string]string{
	"eng": "en", "hrv": "hr", "srp": "sr", "deu": "de", "fra": "fr",
	"ita": "it", "spa": "es", "rus": "ru", "slk": "sk", "slv": "sl",
	"pol": "pl", "ces": "cs", "bul": "bg", "ron": "ro", "ell": "el",
	"ukr": "uk", "por": "pt", "nld": "nl", "tur": "tr", "hun": "hu",
	"ara": "ar", "chi": "zh", "jpn": "ja",
}

func (e *HTMLEngine) fetchHrcak(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	recencyYear := time.Now().UTC().Year() - 3
	u := "https://hrcak.srce.hr/oai/?verb=ListRecords" +
		"&metadataPrefix=oai_dc&from=" + strconv.Itoa(recencyYear) + "-01-01"
	var items []RawArticle
	maxPages := envInt("HRCAK_OAI_MAX_PAGES", 8)
	for page := 0; page < maxPages; page++ {
		xmlText, err := requestText(ctx, e.Browser, "hrcak", u, nil, "", 0, ocrLanguage("en"))
		if err != nil {
			if isConnectorError(err) {
				break
			}
			return nil, err
		}
		parsed, token := parseHrcakRecords(xmlText, query, limit-len(items))
		items = append(items, parsed...)
		if len(items) >= limit || token == "" {
			break
		}
		u = "https://hrcak.srce.hr/oai/?verb=ListRecords&resumptionToken=" + quotePlus(token)
	}
	return items[:minInt(limit, len(items))], nil
}

// parseHrcakRecords parses one harvest page, keeping only query-relevant
// records; returns the resumption token.
func parseHrcakRecords(xmlText, query string, remaining int) ([]RawArticle, string) {
	records, token, err := parseOAI(xmlText)
	if err != nil {
		return nil, ""
	}
	var items []RawArticle
	for _, rec := range records {
		if rec.Deleted {
			continue
		}
		title := rec.first("title")
		if title == "" {
			continue
		}
		description := rec.first("description")
		subjects := rec.Fields["subject"]
		identifiers := rec.Fields["identifier"]
		urlValue := ""
		for _, ident := range identifiers {
			if strings.HasPrefix(ident, "http") {
				urlValue = ident
				break
			}
		}
		dateValue := rec.first("date")
		var authors []string
		seenAuthors := make(map[string]bool)
		for _, name := range rec.Fields["creator"] {
			name = strings.TrimSpace(name)
			if name != "" && !seenAuthors[name] {
				authors = append(authors, name)
				seenAuthors[name] = true
			}
		}
		journal := "Hrčak"
		for _, source := range rec.Fields["source"] {
			if !strings.HasPrefix(strings.ToUpper(source), "ISSN") {
				journal = source
				break
			}
		}
		language := hrcakLanguageMap[strings.ToLower(strings.TrimSpace(rec.first("language")))]
		combined := strings.Join([]string{
			title, description, strings.Join(subjects, " "),
			strings.Join(identifiers, " "), dateValue,
		}, " ")
		if !MatchAllTerms(query, combined, "") {
			continue
		}
		doi := ExtractDOI(combined)
		year := ExtractYear(combined)
		if title == "" || urlValue == "" {
			continue
		}
		if !IsArticleLikeItem(title, urlValue, doi, year) {
			continue
		}
		items = append(items, buildRaw(mustProfile("hrcak"), title, urlValue,
			description, combined, doi, journal, year, authors, "", "", "", language))
		if len(items) >= remaining {
			break
		}
	}
	return items, token
}

// ---------------------------------------------------------------------------
// AJOL (OAI harvest + HTML search with open-access filtering).
// ---------------------------------------------------------------------------

const (
	ajolOAIBase         = "https://www.ajol.info/index.php/ajol/oai"
	ajolPageRangeMaxLen = 12
)

var ajolOAPositiveMarkers = []string{
	"open access", "free access", "download full text", "creative commons", "cc by",
}

var ajolOANegativeMarkers = []string{
	"subscription required", "subscription content only", "purchase", "buy article", "paywall",
}

var ajolPageRangeRe = regexp.MustCompile(`^\d+\s*[-–]\s*\d+$`)

func (e *HTMLEngine) fetchAJOL(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	items, err := e.fetchAJOLOAI(ctx, query, limit)
	if err != nil {
		if isConnectorError(err) {
			return e.fetchAJOLHTML(ctx, query, limit)
		}
		return nil, err
	}
	if len(items) > 0 {
		return items, nil
	}
	return e.fetchAJOLHTML(ctx, query, limit)
}

// fetchAJOLOAI harvests up to 3 ListRecords pages (parity with _fetch_oai).
func (e *HTMLEngine) fetchAJOLOAI(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := ajolOAIBase + "?verb=ListRecords&metadataPrefix=oai_dc"
	var items []RawArticle
	for page := 0; page < 3; page++ {
		xmlText, err := requestText(ctx, e.Browser, "ajol", u, nil, "", 0, ocrLanguage("en"))
		if err != nil {
			return nil, err
		}
		parsed, token := parseAJOLRecords(xmlText, limit-len(items))
		items = append(items, parsed...)
		if len(items) >= limit || token == "" {
			break
		}
		u = ajolOAIBase + "?verb=ListRecords&resumptionToken=" + quotePlus(token)
	}
	return items[:minInt(limit, len(items))], nil
}

// parseAJOLRecords parses one OAI page: deleted records are skipped, the
// rights field gates open access, and the title must reach _MIN_TITLE_LENGTH.
func parseAJOLRecords(xmlText string, remaining int) ([]RawArticle, string) {
	records, token, err := parseOAI(xmlText)
	if err != nil {
		return nil, ""
	}
	var candidates []RawArticle
	var relevant []RawArticle
	for _, rec := range records {
		if rec.Deleted {
			continue
		}
		title := rec.first("title")
		if title == "" {
			continue
		}
		description := rec.first("description")
		identifiers := rec.Fields["identifier"]
		rights := rec.Fields["rights"]
		rightsText := strings.ToLower(strings.Join(rights, " "))
		if len(rights) > 0 && !isOpenAccessText(rightsText) {
			continue
		}
		urlValue := ""
		for _, ident := range identifiers {
			if strings.HasPrefix(ident, "http") {
				urlValue = ident
				break
			}
		}
		if urlValue == "" {
			continue
		}
		journal := "AJOL"
		if sources := rec.Fields["source"]; len(sources) > 0 {
			journal = sources[0]
		}
		combined := strings.Join([]string{title, description, strings.Join(identifiers, " "), journal, rightsText}, " ")
		doi := ExtractDOI(combined)
		year := ExtractYear(combined)
		if utf8Len(title) < minTitleLength {
			continue
		}
		raw := buildRaw(mustProfile("ajol"), title, urlValue, description, combined,
			doi, journal, year, nil, "", "", "", "")
		candidates = append(candidates, raw)
		relevant = append(relevant, raw)
		if len(candidates) >= remaining*3 {
			break
		}
	}
	items := relevant
	if len(items) == 0 {
		items = candidates
	}
	if len(items) > remaining {
		items = items[:remaining]
	}
	return items, token
}

// fetchAJOLHTML runs the AJOL keyword search page, enriching ajol.info items
// and filtering by open access (parity with _extract_from_html in fetch
// context: fetch-level enrichment is repeated by the service's enrich pass).
func (e *HTMLEngine) fetchAJOLHTML(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	profile := mustProfile("ajol")
	htmlBody, err := requestText(ctx, e.Browser, "ajol", profile.SearchURL,
		map[string]string{profile.QueryParam: query}, "", 0, ocrLanguage(profile.Language))
	if err != nil {
		return nil, err
	}
	root, err := parseHTMLBody(htmlBody)
	if err != nil {
		return nil, fetchErr("ajol", "invalid HTML: %v", err)
	}
	if err := assertPageParseable("ajol", htmlBody, root); err != nil {
		return nil, err
	}
	candidates := extractAJOLFromHTML(profile, root, limit)
	if len(candidates) == 0 {
		candidates = genericExtractFromHTML(profile, root, limit*4)
	}
	var relevant []RawArticle
	for _, item := range candidates {
		enriched := &item
		var err error
		if strings.Contains(item.URL, "ajol.info/") {
			enriched, err = e.enrichAJOL(ctx, &item)
			if err != nil {
				return nil, err
			}
			if enriched == nil {
				continue
			}
		}
		text := strings.ToLower(strings.Join([]string{enriched.Title, enriched.Abstract, enriched.FullText}, " "))
		if isOpenAccessText(text) {
			relevant = append(relevant, *enriched)
		}
		if len(relevant) >= limit {
			break
		}
	}
	return relevant[:minInt(limit, len(relevant))], nil
}

// extractAJOLFromHTML parses the article-summary rows (parity with the AJOL
// custom _extract_from_html row handling).
func extractAJOLFromHTML(profile SourceProfile, root *html.Node, limit int) []RawArticle {
	rows := selectNodes(root, ".article-summary.media, .article-summary")
	candidates := make([]RawArticle, 0, limit)
	for _, row := range rows {
		titleNodes := selectNodes(row, "h4.media-heading a, h3 a, h2 a, a[href]")
		if len(titleNodes) == 0 {
			continue
		}
		title := strings.TrimSpace(nodeText(titleNodes[0]))
		href := resolveURL(profile.SearchURL, nodeAttr(titleNodes[0], "href"))
		abstract := ""
		if absNodes := selectNodes(row, ".plugins_generic_lucene_highlighting, .summary, .abstract, p"); len(absNodes) > 0 {
			abstract = strings.TrimSpace(nodeText(absNodes[0]))
		}
		journal := profile.SourceKey
		if jNodes := selectNodes(row, ".meta .journal, .journal, .source"); len(jNodes) > 0 {
			if j := strings.TrimSpace(nodeText(jNodes[0])); j != "" {
				journal = j
			}
		}
		combined := strings.Join([]string{title, abstract, journal, nodeText(row)}, " ")
		doi := ExtractDOI(combined)
		year := ExtractYear(combined)
		if utf8Len(title) < minTitleLength || !strings.HasPrefix(href, "http") {
			continue
		}
		candidates = append(candidates, buildRaw(profile, title, href, abstract, combined,
			doi, journal, year, nil, "", "", "", ""))
		if len(candidates) >= limit*4 {
			break
		}
	}
	return candidates
}

// enrichAJOL enriches via the base landing-page pass plus the article-page
// abstract/authors pass (parity with AJOLConnector.enrich_raw).
func (e *HTMLEngine) enrichAJOL(ctx context.Context, raw *RawArticle) (*RawArticle, error) {
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
	htmlBody, err := requestText(ctx, e.Browser, "ajol", raw.URL, nil, "", 0, ocrLanguage(raw.Language))
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
	pageText := strings.ToLower(htmlText(root))
	doi := enriched.DOI
	if doi == "" {
		doi = ExtractDOI(pageText)
	}
	year := enriched.Year
	if year == nil {
		if y := ExtractYear(pageText); y > 0 {
			year = intPtrOrNil(y)
		}
	}
	journal := enriched.Journal
	if strings.EqualFold(strings.TrimSpace(journal), raw.SourceKey) {
		if j := extractMetaContent(root, []string{
			"citation_journal_title", "dc.source", "prism.publicationname",
		}); j != "" {
			journal = j
		}
	}
	abstract := enriched.Abstract
	pageAbstract := extractAJOLArticleAbstract(root)
	if pageAbstract != "" && (abstract == "" || looksLikePageRange(abstract)) {
		abstract = pageAbstract
	}
	authors := enriched.Authors
	if len(authors) == 0 {
		authors = extractAJOLCitationAuthors(root)
	}
	out := *enriched
	out.DOI = doi
	out.Year = year
	out.Journal = NormalizeScholarly(journal, 300)
	out.Abstract = NormalizeScholarly(abstract, -1)
	out.Authors = authors
	out.FullText = NormalizeScholarly(sliceLen(strings.Join([]string{enriched.FullText, sliceLen(pageText, 12000)}, " "), 20000), -1)
	return &out, nil
}

// extractAJOLArticleAbstract prefers the abstract <p> inside the article
// containers, then the bare container, then the citation_abstract meta
// (parity with _extract_article_abstract).
func extractAJOLArticleAbstract(root *html.Node) string {
	if node := selectNodes(root, "div.article-abstract p, div.abstract p, section.abstract p"); len(node) > 0 {
		if text := strings.TrimSpace(nodeText(node[0])); text != "" {
			return sliceLen(text, 4000)
		}
	}
	if node := selectNodes(root, "div.article-abstract"); len(node) > 0 {
		if text := strings.TrimSpace(nodeText(node[0])); text != "" {
			return sliceLen(text, 4000)
		}
	}
	return extractMetaContent(root, []string{"citation_abstract"})
}

// extractAJOLCitationAuthors returns ordered, de-duplicated citation_author
// meta values (parity with _extract_citation_authors).
func extractAJOLCitationAuthors(root *html.Node) []string {
	var names []string
	seen := make(map[string]bool)
	for _, meta := range selectNodes(root, "meta[name='citation_author']") {
		name := strings.TrimSpace(nodeAttr(meta, "content"))
		if name != "" && !seen[name] {
			names = append(names, name)
			seen[name] = true
		}
	}
	return names
}

// looksLikePageRange reports whether text is just a page range like "8-16".
func looksLikePageRange(text string) bool {
	t := strings.TrimSpace(text)
	if t == "" || utf8Len(t) > ajolPageRangeMaxLen {
		return false
	}
	return ajolPageRangeRe.MatchString(t)
}

// isOpenAccessText applies the AJOL open-access marker rules with a default
// of true when no marker is present (parity with _is_open_access_text).
func isOpenAccessText(text string) bool {
	lowered := strings.ToLower(text)
	for _, token := range ajolOANegativeMarkers {
		if strings.Contains(lowered, token) {
			return false
		}
	}
	for _, token := range ajolOAPositiveMarkers {
		if strings.Contains(lowered, token) {
			return true
		}
	}
	return true
}

// envInt reads an integer environment variable with a default.
func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
