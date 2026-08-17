package service

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/4q4r/cindex/backend/internal/connector"
	"github.com/4q4r/cindex/backend/internal/domain"
)

// Section headers written by LocalArticleStore.save (parity with
// _SECTION_* in apps.extraction.local_store).
const (
	sectionTLDR     = "## TLDR"
	sectionAbstract = "## Аннотация"
	sectionFullText = "## Полный текст"
	sectionFormulas = "## Формулы"
	sectionFigures  = "## Графики и рисунки"
	sectionQuotes   = "## Извлечённые цитаты"
)

var safeFilenameRe = regexp.MustCompile(`[^A-Za-z0-9._-]`)
var frontMatterCloseRe = regexp.MustCompile(`\n---[ \t\r]*\n`)

// FrozenQuote is one parsed quote from the frozen md (parity with the quote
// dicts produced by _parse_quotes_section).
type FrozenQuote struct {
	Text      string
	Location  string
	Relevance float64
	Rationale string
}

// LocalStore is the filesystem-backed markdown store for PERELMAN-processed
// articles (parity with LocalArticleStore in apps.extraction.local_store).
// Lookups key off the article DOI: a “10.“-prefixed key maps to
// “{doi_with_slashes_as_underscores}.md“, any other key is sanitized.
type LocalStore struct {
	dir string
}

// NewLocalStore builds the store over CINDEX_ARTICLES_DIR.
func NewLocalStore(dir string) *LocalStore {
	return &LocalStore{dir: dir}
}

// Path returns the absolute “.md“ path for a DOI or key.
func (s *LocalStore) Path(doiOrKey string) string {
	key := strings.TrimSpace(doiOrKey)
	var safe string
	if strings.HasPrefix(key, "10.") {
		safe = strings.ReplaceAll(key, "/", "_")
	} else {
		safe = sanitizeFilename(key)
	}
	return filepath.Join(s.dir, safe+".md")
}

func sanitizeFilename(name string) string {
	safe := safeFilenameRe.ReplaceAllString(name, "_")
	safe = strings.Trim(safe, "._-")
	if safe == "" {
		return "article"
	}
	return safe
}

// Exists reports whether a frozen md file exists for the DOI.
func (s *LocalStore) Exists(doi string) bool {
	if doi == "" {
		return false
	}
	info, err := os.Stat(s.Path(doi))
	return err == nil && !info.IsDir()
}

// ToRaw builds a RawArticle from the frozen md, merging onto fallback.
// Returns nil when the file is missing or the front matter is malformed so
// the caller can fall back to network enrichment.
func (s *LocalStore) ToRaw(doi string, fallback connector.RawArticle) *connector.RawArticle {
	if doi == "" {
		return nil
	}
	rawText, err := os.ReadFile(s.Path(doi))
	if err != nil {
		return nil
	}
	front, body := parseFrozenMD(string(rawText))
	if front == nil {
		return nil
	}
	abstract := sectionBody(body, sectionAbstract)
	if abstract == "" {
		abstract = fallback.Abstract
	}
	fullText := sectionBody(body, sectionFullText)
	if fullText == "" {
		fullText = fallback.FullText
	}
	year := fallback.Year
	if y, ok := front["year"]; ok {
		if n, err2 := strconv.Atoi(y); err2 == nil {
			year = intPtr(n)
		}
	}
	authors := fallback.Authors
	if list, ok := front["authors"]; ok && list != "" {
		parts := strings.Split(list, "\x00")
		clean := parts[:0]
		for _, p := range parts {
			if p != "" {
				clean = append(clean, p)
			}
		}
		if len(clean) > 0 {
			authors = clean
		}
	}
	merged := fallback
	if v := front["title"]; v != "" {
		merged.Title = v
	}
	merged.Abstract = abstract
	merged.FullText = fullText
	if v := front["doi"]; v != "" {
		merged.DOI = v
	}
	if v := front["url"]; v != "" {
		merged.URL = v
	}
	if v := front["journal"]; v != "" {
		merged.Journal = v
	}
	if v := front["source_key"]; v != "" {
		merged.SourceKey = v
	}
	merged.Year = year
	merged.Authors = authors
	return &merged
}

// ReadQuotes returns the parsed "## Извлечённые цитаты" section. A missing
// file or malformed md is a cache miss (nil); an absent section is an empty
// cached result.
func (s *LocalStore) ReadQuotes(doi string) []FrozenQuote {
	if doi == "" {
		return nil
	}
	rawText, err := os.ReadFile(s.Path(doi))
	if err != nil {
		return nil
	}
	front, body := parseFrozenMD(string(rawText))
	if front == nil {
		return nil
	}
	if !hasSectionHeader(body, sectionQuotes) {
		return []FrozenQuote{}
	}
	return parseQuotesSection(sectionBody(body, sectionQuotes))
}

// Save freezes a published PERELMAN result to markdown and returns its path
// relative to the articles directory.
func (s *LocalStore) Save(article *domain.Article, sourceKey, journal string, authors []string, result ExtractionResult) (string, error) {
	if article == nil || article.DOI == "" {
		return "", errors.New("local store: article DOI is required")
	}
	if err := os.MkdirAll(s.dir, 0o750); err != nil {
		return "", fmt.Errorf("local store: create directory: %w", err)
	}
	path := s.Path(article.DOI)
	temporary, err := os.CreateTemp(s.dir, ".article-*.tmp")
	if err != nil {
		return "", fmt.Errorf("local store: create temporary markdown: %w", err)
	}
	temporaryPath := temporary.Name()
	defer func() { _ = os.Remove(temporaryPath) }()
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return "", fmt.Errorf("local store: secure temporary markdown: %w", err)
	}
	if _, err := temporary.WriteString(renderFrozenMD(article, sourceKey, journal, authors, result)); err != nil {
		_ = temporary.Close()
		return "", fmt.Errorf("local store: write temporary markdown: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return "", fmt.Errorf("local store: sync temporary markdown: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return "", fmt.Errorf("local store: close temporary markdown: %w", err)
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return "", fmt.Errorf("local store: publish markdown: %w", err)
	}
	relative, err := filepath.Rel(s.dir, path)
	if err != nil {
		return "", fmt.Errorf("local store: relative path: %w", err)
	}
	return filepath.ToSlash(relative), nil
}

func renderFrozenMD(article *domain.Article, sourceKey, journal string, authors []string, result ExtractionResult) string {
	var b strings.Builder
	b.WriteString("---\n")
	writeFrontValue(&b, "title", article.Title)
	if len(authors) > 0 {
		b.WriteString("authors:\n")
		for _, author := range authors {
			if author = strings.TrimSpace(author); author != "" {
				fmt.Fprintf(&b, "  - %s\n", collapseWhitespace(author))
			}
		}
	}
	if article.PubYear != nil {
		fmt.Fprintf(&b, "year: %d\n", *article.PubYear)
	}
	writeFrontValue(&b, "journal", journal)
	writeFrontValue(&b, "doi", article.DOI)
	writeFrontValue(&b, "url", article.URL)
	writeFrontValue(&b, "source_key", sourceKey)
	b.WriteString("is_preprint: false\n---\n\n")
	if result.TLDR != "" {
		b.WriteString(sectionTLDR + "\n\n" + collapseWhitespace(result.TLDR) + "\n\n")
	}
	b.WriteString(sectionAbstract + "\n\n" + strings.TrimSpace(article.Abstract) + "\n\n")
	b.WriteString(sectionFullText + "\n\n" + strings.TrimSpace(article.FullText) + "\n\n")
	b.WriteString(sectionFormulas + "\n")
	for _, formula := range result.Formulas {
		if formula.Latex == "" {
			continue
		}
		fmt.Fprintf(&b, "\n- %s", formula.Latex)
		if formula.Location != "" {
			fmt.Fprintf(&b, "  \n  — location: %s", formula.Location)
		}
		if formula.Caption != "" {
			fmt.Fprintf(&b, "  \n  — caption: %s", formula.Caption)
		}
		b.WriteByte('\n')
	}
	b.WriteString("\n" + sectionFigures + "\n")
	for _, figure := range result.Figures {
		if figure.Markdown == "" {
			continue
		}
		kind := figure.Kind
		if kind == "" {
			kind = "figure"
		}
		fmt.Fprintf(&b, "\n### %s\n", kind)
		if figure.Location != "" {
			fmt.Fprintf(&b, "*location: %s*\n", figure.Location)
		}
		b.WriteString(figure.Markdown + "\n")
		if figure.Caption != "" {
			fmt.Fprintf(&b, "\n*%s*\n", figure.Caption)
		}
	}
	b.WriteString("\n" + sectionQuotes + "\n")
	for _, quote := range result.Quotes {
		if quote.Text == "" {
			continue
		}
		fmt.Fprintf(&b, "\n- text: %s\n", collapseWhitespace(quote.Text))
		if quote.Location != "" {
			fmt.Fprintf(&b, "  location: %s\n", quote.Location)
		}
		fmt.Fprintf(&b, "  relevance: %g\n", quote.Relevance)
		if quote.Rationale != "" {
			fmt.Fprintf(&b, "  rationale: %s\n", quote.Rationale)
		}
	}
	return strings.TrimRight(b.String(), "\n") + "\n"
}

func writeFrontValue(b *strings.Builder, key, value string) {
	if value = collapseWhitespace(value); value != "" {
		fmt.Fprintf(b, "%s: %s\n", key, value)
	}
}

func collapseWhitespace(value string) string {
	return strings.Join(strings.Fields(value), " ")
}

// parseFrozenMD splits the md into (front-matter map, body). Returns
// (nil, "") when the front-matter delimiters are malformed. Values are
// normalized to strings; the authors list is joined with NUL separators.
func parseFrozenMD(text string) (map[string]string, string) {
	if !strings.HasPrefix(text, "---") {
		return map[string]string{}, text
	}
	close := frontMatterCloseRe.FindStringIndex(text)
	if close == nil {
		return nil, ""
	}
	frontText := text[3:close[0]]
	body := text[close[1]:]
	return parseFrontMatter(frontText), body
}

func parseFrontMatter(frontText string) map[string]string {
	out := make(map[string]string)
	currentList := ""
	var listItems []string
	for _, rawLine := range strings.Split(frontText, "\n") {
		line := strings.TrimRight(rawLine, "\r")
		stripped := strings.TrimSpace(line)
		if stripped == "" {
			if currentList != "" && len(listItems) > 0 {
				out[currentList] = strings.Join(listItems, "\x00")
				listItems = nil
			}
			currentList = ""
			continue
		}
		if strings.HasPrefix(stripped, "- ") {
			if currentList != "" {
				listItems = append(listItems, strings.TrimSpace(stripped[2:]))
			}
			continue
		}
		colon := strings.Index(line, ":")
		if colon == -1 {
			continue
		}
		key := strings.TrimSpace(line[:colon])
		value := strings.TrimSpace(line[colon+1:])
		if value == "" {
			if currentList != "" && len(listItems) > 0 {
				out[currentList] = strings.Join(listItems, "\x00")
			}
			currentList = key
			listItems = nil
			out[key] = ""
		} else {
			if currentList != "" && len(listItems) > 0 {
				out[currentList] = strings.Join(listItems, "\x00")
				listItems = nil
			}
			currentList = ""
			out[key] = value
		}
	}
	if currentList != "" && len(listItems) > 0 {
		out[currentList] = strings.Join(listItems, "\x00")
	}
	return out
}

// sectionBody returns the text under header up to the next "## " section
// (parity with _section_body). Returns "" when the header is absent.
func sectionBody(body, header string) string {
	idx := strings.Index(body, "\n"+header)
	if idx == -1 {
		if !strings.HasPrefix(body, header) {
			return ""
		}
		idx = 0
	}
	start := idx + len(header) + 1
	if idx == 0 {
		start = len(header)
	}
	nextIdx := strings.Index(body[start:], "\n## ")
	if nextIdx != -1 {
		return strings.Trim(strings.TrimSpace(body[start:start+nextIdx]), "\n")
	}
	return strings.TrimSpace(body[start:])
}

func hasSectionHeader(body, header string) bool {
	return strings.HasPrefix(body, header) || strings.Contains(body, "\n"+header)
}

// parseQuotesSection parses "- text: ..." items with indented
// location/relevance/rationale sub-fields (parity with _parse_quotes_section).
func parseQuotesSection(text string) []FrozenQuote {
	var out []FrozenQuote
	var current *FrozenQuote
	for _, rawLine := range strings.Split(text, "\n") {
		if strings.TrimSpace(rawLine) == "" {
			continue
		}
		if strings.HasPrefix(rawLine, "- ") {
			if current != nil {
				out = append(out, *current)
			}
			item := strings.TrimSpace(rawLine[2:])
			item = strings.TrimPrefix(item, "text:")
			current = &FrozenQuote{Text: strings.TrimSpace(item)}
		} else if current != nil && strings.HasPrefix(rawLine, "  ") {
			applyQuoteField(current, strings.TrimSpace(rawLine))
		}
	}
	if current != nil {
		out = append(out, *current)
	}
	return out
}

func applyQuoteField(q *FrozenQuote, line string) {
	colon := strings.Index(line, ":")
	if colon == -1 {
		return
	}
	key := strings.TrimSpace(line[:colon])
	value := strings.TrimSpace(line[colon+1:])
	switch key {
	case "location":
		q.Location = value
	case "rationale":
		q.Rationale = value
	case "relevance":
		if f, err := strconv.ParseFloat(value, 64); err == nil {
			q.Relevance = f
		}
	}
}

func intPtr(v int) *int { return &v }
