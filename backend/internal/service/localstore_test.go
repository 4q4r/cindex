package service

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/4q4r/cindex/backend/internal/connector"
	"github.com/4q4r/cindex/backend/internal/domain"
)

func TestLocalStorePathRules(t *testing.T) {
	s := NewLocalStore("/var/articles")
	cases := []struct {
		key, want string
	}{
		{"10.5555/abc.def", "/var/articles/10.5555_abc.def.md"},
		{"10.1000/foo/bar", "/var/articles/10.1000_foo_bar.md"},
		{"https://x.org/1 2?", "/var/articles/https___x.org_1_2.md"},
		{"", "/var/articles/article.md"},
	}
	for _, c := range cases {
		if got := s.Path(c.key); got != c.want {
			t.Errorf("Path(%q) = %q, want %q", c.key, got, c.want)
		}
	}
}

func TestLocalStoreExistsAndToRaw(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalStore(dir)
	if s.Exists("10.1/x") {
		t.Fatal("Exists must be false for a missing file")
	}
	md := `---
title: Frozen Title
authors:
- Alice Smith
- Bob Jones
year: 2021
doi: 10.1/x
---
## Аннотация
Frozen abstract text.
## Полный текст
Frozen full text.
`
	if err := os.WriteFile(filepath.Join(dir, "10.1_x.md"), []byte(md), 0o644); err != nil {
		t.Fatal(err)
	}
	if !s.Exists("10.1/x") {
		t.Fatal("Exists must be true after writing")
	}
	fallback := connector.RawArticle{
		Title: "Network Title", SourceKey: "ajol", DOI: "10.1/x", URL: "http://x/1",
		Journal: "AJOL", Abstract: "Network abstract", FullText: "Network text",
		Year: intPtrForTest(2020), Authors: []string{"Carol"},
	}
	raw := s.ToRaw("10.1/x", fallback)
	if raw == nil {
		t.Fatal("ToRaw must succeed on a valid file")
	}
	if raw.Title != "Frozen Title" {
		t.Errorf("title = %q, want frozen", raw.Title)
	}
	if raw.Abstract != "Frozen abstract text." {
		t.Errorf("abstract = %q, want frozen", raw.Abstract)
	}
	if raw.FullText != "Frozen full text." {
		t.Errorf("full_text = %q, want frozen", raw.FullText)
	}
	if raw.Year == nil || *raw.Year != 2021 {
		t.Errorf("year = %v, want 2021", raw.Year)
	}
	if len(raw.Authors) != 2 || raw.Authors[0] != "Alice Smith" {
		t.Errorf("authors = %v, want [Alice Smith Bob Jones]", raw.Authors)
	}
	// Fields absent from the front matter fall through to the network payload.
	if raw.SourceKey != "ajol" || raw.Journal != "AJOL" || raw.URL != "http://x/1" {
		t.Errorf("fallback fields lost: %+v", raw)
	}
}

func TestLocalStoreToRawMalformed(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalStore(dir)
	fallback := connector.RawArticle{Title: "T", DOI: "10.1/y"}
	if err := os.WriteFile(filepath.Join(dir, "10.1_y.md"), []byte("---\ntitle: missing delimiter"), 0o644); err != nil {
		t.Fatal(err)
	}
	if raw := s.ToRaw("10.1/y", fallback); raw != nil {
		t.Errorf("ToRaw on malformed md = %+v, want nil", raw)
	}
}

func TestLocalStoreReadQuotes(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalStore(dir)
	md := `---
title: Q
---
## Извлечённые цитаты
- text: First quote
  location: p. 3
  relevance: 0.87
  rationale: Strong claim
- text: Second quote
  relevance: 0.5
`
	if err := os.WriteFile(filepath.Join(dir, "10.2_q.md"), []byte(md), 0o644); err != nil {
		t.Fatal(err)
	}
	quotes := s.ReadQuotes("10.2/q")
	if len(quotes) != 2 {
		t.Fatalf("ReadQuotes = %d quotes, want 2", len(quotes))
	}
	q1 := quotes[0]
	if q1.Text != "First quote" || q1.Location != "p. 3" || q1.Relevance != 0.87 || q1.Rationale != "Strong claim" {
		t.Errorf("quote[0] = %+v", q1)
	}
	if quotes[1].Location != "" || quotes[1].Relevance != 0.5 {
		t.Errorf("quote[1] = %+v", quotes[1])
	}
	if s.ReadQuotes("10.2/missing") != nil {
		t.Error("ReadQuotes on missing file must be nil")
	}
}

func TestLocalStoreReadQuotesEmptySection(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalStore(dir)
	if err := os.WriteFile(filepath.Join(dir, "10.3_x.md"), []byte("---\ntitle: X\n---\n## TLDR\nnothing\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if quotes := s.ReadQuotes("10.3/x"); quotes == nil || len(quotes) != 0 {
		t.Errorf("ReadQuotes without a quotes section = %v, want empty slice", quotes)
	}
}

func TestLocalStoreReadQuotesFrontMatterOnly(t *testing.T) {
	dir := t.TempDir()
	s := NewLocalStore(dir)
	if err := os.WriteFile(filepath.Join(dir, "10.4_x.md"), []byte("---\ntitle: X\n---\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if quotes := s.ReadQuotes("10.4/x"); quotes == nil || len(quotes) != 0 {
		t.Errorf("ReadQuotes on front-matter-only md = %v, want empty slice", quotes)
	}
}

func TestParseFrozenMDFrontMatterListFlush(t *testing.T) {
	front, body := parseFrozenMD("---\nauthors:\n- A\n- B\ntitle: T\n---\nbody text")
	if front == nil {
		t.Fatal("front matter must parse")
	}
	if front["authors"] != "A\x00B" || front["title"] != "T" {
		t.Errorf("front = %#v", front)
	}
	if !strings.Contains(body, "body text") {
		t.Errorf("body = %q", body)
	}
}

func TestLocalStoreSaveRoundTrip(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "articles")
	s := NewLocalStore(dir)
	year := 2025
	article := &domain.Article{
		Title: "Frozen", Abstract: "Abstract", FullText: "Body", PubYear: &year,
		DOI: "10.5/frozen", URL: "https://example.test/frozen",
	}
	result := ExtractionResult{
		TLDR:     "Кратко\nв одну строку",
		Quotes:   []domain.Quote{{Text: "Quote", Location: "p. 1", Relevance: 0.9, Rationale: "Core"}},
		Formulas: []Formula{{Latex: "$$x=1$$", Location: "p. 2"}},
		Figures:  []Figure{{Markdown: "|x|y|", Kind: "table", Caption: "Data"}},
	}
	relative, err := s.Save(article, "ajol", "Journal", []string{"A. Author"}, result)
	if err != nil {
		t.Fatal(err)
	}
	if relative != "10.5_frozen.md" || !s.Exists(article.DOI) {
		t.Fatalf("saved path = %q", relative)
	}
	raw := s.ToRaw(article.DOI, connector.RawArticle{})
	if raw == nil || raw.Title != article.Title || raw.Abstract != article.Abstract || raw.FullText != article.FullText || len(raw.Authors) != 1 {
		t.Fatalf("round-trip raw = %+v", raw)
	}
	quotes := s.ReadQuotes(article.DOI)
	if len(quotes) != 1 || quotes[0].Relevance != 0.9 {
		t.Fatalf("round-trip quotes = %+v", quotes)
	}
	content, err := os.ReadFile(s.Path(article.DOI))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{sectionTLDR, sectionFormulas, "$$x=1$$", sectionFigures, "### table"} {
		if !strings.Contains(string(content), want) {
			t.Errorf("saved markdown missing %q", want)
		}
	}
}

func intPtrForTest(v int) *int { return &v }
