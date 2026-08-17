package service

import (
	"testing"

	"github.com/4q4r/cindex/backend/internal/repository"
)

func TestNormalizeScholarlyText(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"  Hello   world  ", "Hello world"},
		{"<p>Hello <b>world</b></p>", "Hello world"},
		{"Hello &amp; goodbye", "Hello & goodbye"},
		{"Line1\nLine2\n\nLine3", "Line1 Line2 Line3"},
		{"\tTabs\tand\vcontrols\x00", "Tabs and controls"},
		{"<script>alert(1)</script>clean", "alert(1) clean"}, // tags stripped, content kept (Django HTML_TAG_RE)
		{"", ""},
	}
	for _, tc := range cases {
		if got := NormalizeScholarlyText(tc.in, 1000); got != tc.want {
			t.Errorf("NormalizeScholarlyText(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}

	// Truncation applies on top of normalization.
	got := NormalizeScholarlyText("one two three four", 10)
	if len(got) > 10 {
		t.Errorf("truncation: %q (%d)", got, len(got))
	}
}

func TestCanonicalTextKey(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"  Hello, WORLD!  ", "hello world"},
		{"Multi-word    title", "multi word title"},
		{"J. of X", "j of x"}, // punctuation is non-word (Django NON_WORD_RE)
		{"Длинное название", "длинное название"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := CanonicalTextKey(tc.in); got != tc.want {
			t.Errorf("CanonicalTextKey(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestNormalizeDOI(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"10.1234/ABC", "10.1234/abc"},
		// Parity: normalize_doi does NOT strip the doi.org prefix.
		{"  https://doi.org/10.1234/abc  ", "https://doi.org/10.1234/abc"},
		{"10.1234/", "10.1234/"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := NormalizeDOI(tc.in); got != tc.want {
			t.Errorf("NormalizeDOI(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestSearchTextCombine(t *testing.T) {
	s := &Search{}
	if got := s.SearchText("deep learning", "neural networks"); got != "deep learning neural networks" {
		t.Errorf("SearchText = %q", got)
	}
	if got := s.SearchText("query", ""); got != "query" {
		t.Errorf("SearchText(no expression) = %q", got)
	}
	if got := s.SearchText("", ""); got != "" {
		t.Errorf("SearchText(empty) = %q", got)
	}
}

func TestSearchTerms(t *testing.T) {
	s := &Search{}
	terms := s.SearchTerms("Deep  Learning", "")
	if len(terms) != 2 || terms[0] != "deep" || terms[1] != "learning" {
		t.Errorf("terms = %v", terms)
	}
	// Cyrillic casefold (parity with Python str.casefold).
	terms = s.SearchTerms("Квантовые вычисления", "")
	if terms[0] != "квантовые" {
		t.Errorf("cyrillic terms = %v", terms)
	}
}

func TestDedupeKey(t *testing.T) {
	s := &Search{}
	year := 2023
	doiRow := &repository.SearchRow{DOI: "10.1234/ABC"}
	if got := s.dedupeKey(doiRow); got != "doi:10.1234/abc" {
		t.Errorf("doi dedupe = %q", got)
	}
	noDOI := &repository.SearchRow{Title: "  A  TITLE! ", Year: &year, Journal: "J. of X"}
	if got := s.dedupeKey(noDOI); got != "title:a title|year:2023|journal:j of x" {
		t.Errorf("title dedupe = %q", got)
	}
}
