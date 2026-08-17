package connector

import (
	"regexp"
	"strings"
	"time"
)

// Pattern parity with apps.ingestion.connectors.base.
var (
	doiPattern     = regexp.MustCompile(`(?i)10\.\d{4,9}/[-._;()/:A-Z0-9]+`)
	yearPattern    = regexp.MustCompile(`(?:19|20)\d{2}`)
	pdfURLPattern  = regexp.MustCompile(`(?i)https?://[^\s"'<>]+?\.pdf(?:\?[^\s"'<>]+)?`)
	nonWordPattern = regexp.MustCompile(`\s+`)
	whiteSpaceRe   = regexp.MustCompile(`\s+`)
	// Go regexp has no lookahead: capture the capital letter that starts the
	// real title and re-insert it after stripping the conference-abstract
	// number (parity with _clean_pmc_title's ``^\d{1,4}\s+(?=[A-Z])``).
	numericPrefixRe = regexp.MustCompile(`^\d{1,4}\s+([A-Z])`)
	wordTokens      = regexp.MustCompile(`[a-zA-Zа-яА-Я0-9]+`)
)

// PeerReviewTokens, IndexingTokens, PreprintTokens mirror the Django evidence
// token sets.
var (
	peerReviewTokens = []string{"peer reviewed", "peer-review", "refereed", "double blind review"}
	indexingTokens   = []string{"scopus", "web of science", "medline", "pmc", "pubmed central", "kci", "tr dizin", "doaj"}
	preprintTokens   = []string{"preprint", "author manuscript", "accepted manuscript", "working paper"}
)

// BadArticleTokens are landing-page navigation titles that never qualify as
// articles (parity with _is_article_like_item; compared as an exact match
// against the normalized title).
var badArticleTokens = []string{
	"browse", "advanced search", "journal collections", "home", "login",
	"about", "help",
}

// currentMaxPublicationYear returns the current year + 1 (Django parity).
func currentMaxPublicationYear() int {
	return time.Now().UTC().Year() + 1
}

// ExtractDOI mirrors _extract_doi: first DOI-pattern match, trailing dot
// stripped.
func ExtractDOI(text string) string {
	if m := doiPattern.FindString(text); m != "" {
		return strings.TrimRight(m, ".")
	}
	return ""
}

// ExtractYear returns the maximum year in [1800, currentYear+1] found in text,
// or 0.
func ExtractYear(text string) int {
	max := 0
	for _, m := range yearPattern.FindAllString(text, -1) {
		y := 0
		for _, ch := range m {
			y = y*10 + int(ch-'0')
		}
		if y >= 1800 && y <= currentMaxPublicationYear() && y > max {
			max = y
		}
	}
	return max
}

// ExtractPDFURL mirrors _extract_pdf_url's regex fallback.
func ExtractPDFURL(text string) string {
	return pdfURLPattern.FindString(text)
}

// NormalizeScholarly mirrors normalize_scholarly_text with max length.
func NormalizeScholarly(value string, maxLen int) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	value = nonWordPattern.ReplaceAllString(value, " ")
	value = strings.TrimSpace(value)
	if maxLen >= 0 {
		runes := []rune(value)
		if len(runes) > maxLen {
			value = strings.TrimRight(string(runes[:maxLen]), " \t\n")
		}
	}
	return value
}

// IsArticleLikeItem mirrors _is_article_like_item: title length >= 18,
// exact-match navigation token check, http URL, and a DOI or year present.
func IsArticleLikeItem(title, urlText, doi string, year int) bool {
	if !titleIsArticleLike(title) {
		return false
	}
	if !strings.HasPrefix(urlText, "http") {
		return false
	}
	return doi != "" || year > 0
}

// titleIsArticleLike checks the title length and navigation-token gate shared
// by _is_article_like_item.
func titleIsArticleLike(title string) bool {
	title = strings.TrimSpace(title)
	if len([]rune(title)) < 18 {
		return false
	}
	normalized := strings.ToLower(title)
	for _, tok := range badArticleTokens {
		if normalized == tok {
			return false
		}
	}
	return true
}

// MergeEvidence scans text for evidence tokens and folds matches into the
// existing evidence strings (parity with _merge_evidence: space-joined, dedup
// against the merged string case-insensitively).
func MergeEvidence(text string, peer, indexing, preprint string) (string, string, string) {
	low := strings.ToLower(text)
	peer = mergeTokenSet(low, peer, peerReviewTokens)
	indexing = mergeTokenSet(low, indexing, indexingTokens)
	preprint = mergeTokenSet(low, preprint, preprintTokens)
	return peer, indexing, preprint
}

func mergeTokenSet(lowerText, existing string, tokens []string) string {
	merged := strings.TrimSpace(existing)
	lowerMerged := strings.ToLower(merged)
	for _, tok := range tokens {
		if strings.Contains(lowerText, tok) && !strings.Contains(lowerMerged, tok) {
			merged = strings.TrimSpace(merged + " " + tok)
			lowerMerged = strings.ToLower(merged)
		}
	}
	return merged
}

// MergeEvidenceSkipPreprint is used by PMC where a preprint finding clears the
// peer-review evidence (parity with the PMC connector).
func MergeEvidenceSkipPreprint(text, peer, indexing, preprint string) (string, string, string) {
	low := strings.ToLower(text)
	for _, tok := range preprintTokens {
		if strings.Contains(low, tok) {
			peer = ""
			preprint = tok
		}
	}
	peer, indexing, preprint = MergeEvidence(text, peer, indexing, preprint)
	return peer, indexing, preprint
}

// fullTextFor builds the full-text blob (Django parity: title + abstract when
// an abstract exists, title alone otherwise).
func fullTextFor(title, abstract string) string {
	if abstract != "" {
		return title + " " + abstract
	}
	return title
}

// SplitAuthors splits an author string on comma/semicolon (Django parity).
func SplitAuthors(s string) []string {
	parts := strings.FieldsFunc(s, func(r rune) bool {
		return r == ',' || r == ';'
	})
	var out []string
	for _, p := range parts {
		if t := strings.TrimSpace(p); t != "" {
			out = append(out, t)
		}
	}
	return out
}

// ToInt parses an int defensively (parity with _to_int).
func ToInt(s string) int {
	var n int
	for _, ch := range s {
		if ch < '0' || ch > '9' {
			return 0
		}
		n = n*10 + int(ch-'0')
	}
	return n
}

// MatchAllTerms implements the IACR/OAI _matches_query filter: every
// whitespace token (len > 2) of the query must appear in title+abstract.
func MatchAllTerms(query, title, abstract string) bool {
	var tokens []string
	for _, m := range wordTokens.FindAllString(strings.ToLower(query), -1) {
		if len(m) > 2 {
			tokens = append(tokens, m)
		}
	}
	if len(tokens) == 0 {
		return true // empty token list treated as a match (Django parity)
	}
	blob := strings.ToLower(title + " " + abstract)
	for _, t := range tokens {
		if !strings.Contains(blob, t) {
			return false
		}
	}
	return true
}

// cleanPMCTitle strips a leading conference-abstract number from the title
// (parity with _clean_pmc_title: only for pubTypes containing Abstract or
// Congress, so legitimate numeric-leading titles are untouched).
func cleanPMCTitle(title string, pubTypes []string) string {
	isConferenceAbstract := false
	for _, t := range pubTypes {
		low := strings.ToLower(t)
		if strings.Contains(low, "abstract") || strings.Contains(low, "congress") {
			isConferenceAbstract = true
			break
		}
	}
	if !isConferenceAbstract {
		return strings.TrimSpace(title)
	}
	return numericPrefixRe.ReplaceAllStringFunc(strings.TrimSpace(title), func(m string) string {
		return m[len(m)-1:] // keep the capital letter that begins the real title
	})
}

// Exa abstract-cleaning regexes (parity with ExaConnector._clean_abstract).
var (
	exaBoilerplateRe   = regexp.MustCompile(`(?i)^(?:(?:skip\s+to\s+main\s+content|view\s+pdf|download\s+(?:full\s+)?(?:issue|article|pdf)|search\s+sciencedirect|open\s+access|close|hide\s+(?:modal|popup|notification)|cookies?\s+(?:preferences|settings)|sign\s+in|log\s+in|register|access\s+options|buy\s+(?:this\s+)?article|share\s+(?:this\s+)?article|article\s+metadata|check\s+access|pdf\s+download)\s*[,.]?\s*)+`)
	exaSectionHeaderRe = regexp.MustCompile(`(?im)^(?:###\s*)?(?:Subjects?|Keywords?|References|Bibliography|Cited\s+by|Cite\s+(?:this\s+)?article|Related\s+articles|Download\s+PDF|Share|Figures?|Tables?|Acknowledgments?|Appendix|Supplementary|Copyright|License|Publisher\s+note|Funding|Data\s+availability|Code\s+availability|Ethics\s+declarations?|Author\s+information|Author\s+contributions?|Competing\s+interests?|Additional\s+information|About\s+this\s+article|Comments)\s*:?\s*$|(?:###\s*)(?:Subjects?|Keywords?|References|Bibliography|Cited\s+by|Cite\s+(?:this\s+)?article|Related\s+articles|Download\s+PDF|Share|Figures?|Tables?|Acknowledgments?|Appendix|Supplementary|Copyright|License|Publisher\s+note|Funding|Data\s+availability|Code\s+availability|Ethics\s+declarations?|Author\s+information|Author\s+contributions?|Competing\s+interests?|Additional\s+information|About\s+this\s+article|Comments)\b\s*:?\s*`)
)

// CleanAbstract removes navigation boilerplate and duplicated title from page
// text (parity with ExaConnector._clean_abstract).
func CleanAbstract(rawText, title string) string {
	if rawText == "" {
		return ""
	}
	text := rawText
	textRunes := []rune(text)
	prefixLen := minInt(200, len(textRunes))
	prefix := string(textRunes[:prefixLen])
	cleanedPrefix := exaBoilerplateRe.ReplaceAllString(prefix, "")
	if cleanedPrefix != prefix {
		text = cleanedPrefix + string(textRunes[prefixLen:])
	}
	normTitle := strings.ToLower(NormalizeScholarly(title, -1))
	textRunes = []rune(text)
	titleRunes := []rune(title)
	sampleLen := minInt(len(titleRunes)+50, len(textRunes))
	normStart := strings.ToLower(NormalizeScholarly(string(textRunes[:sampleLen]), -1))
	if normTitle != "" && strings.HasPrefix(normStart, normTitle) {
		cut := minInt(len(titleRunes), len(textRunes))
		text = strings.TrimLeft(string(textRunes[cut:]), " .,-—–")
	}
	text = exaSectionHeaderRe.ReplaceAllString(text, "")
	return strings.TrimSpace(whiteSpaceRe.ReplaceAllString(text, " "))
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// StripHTMLTags removes tags and collapses whitespace (lightweight; the HTML
// connectors use the x/net/html based sanitizer for full pages).
func StripHTMLTags(s string) string {
	var b strings.Builder
	inTag := false
	for _, r := range s {
		switch {
		case r == '<':
			inTag = true
		case r == '>':
			inTag = false
		case !inTag:
			b.WriteRune(r)
		}
	}
	return NormalizeScholarly(b.String(), -1)
}
