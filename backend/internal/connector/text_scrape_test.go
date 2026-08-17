package connector

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestCleanAbstractShortBoilerplate(t *testing.T) {
	if got := CleanAbstract("Open Access useful abstract", ""); got != "useful abstract" {
		t.Errorf("CleanAbstract = %q", got)
	}
}

func TestCleanAbstractUnicodeTitle(t *testing.T) {
	title := "Исследование общественного здоровья"
	got := CleanAbstract(title+". Полезная аннотация исследования.", title)
	if got != "Полезная аннотация исследования." {
		t.Errorf("CleanAbstract = %q", got)
	}
	if !utf8.ValidString(got) {
		t.Fatal("CleanAbstract returned invalid UTF-8")
	}
}

func TestCleanAbstractLongUnicodePrefix(t *testing.T) {
	raw := "Open Access " + strings.Repeat("界", 220)
	got := CleanAbstract(raw, "")
	if !utf8.ValidString(got) || !strings.HasPrefix(got, "界") {
		t.Errorf("CleanAbstract returned invalid or unexpected text: %q", got)
	}
}

func TestNormalizeScholarlyTruncatesRunes(t *testing.T) {
	got := NormalizeScholarly(strings.Repeat("界", 10), 7)
	if utf8.RuneCountInString(got) != 7 || !utf8.ValidString(got) {
		t.Errorf("NormalizeScholarly = %q", got)
	}
}

func TestResolveURL(t *testing.T) {
	cases := []struct {
		base string
		ref  string
		want string
	}{
		{"https://example.org/search", "/article/1", "https://example.org/article/1"},
		{"https://example.org/path/search", "article/1", "https://example.org/path/article/1"},
		{"https://example.org/search", "//cdn.example.org/a", "https://cdn.example.org/a"},
		{"https://example.org/search?q=old", "?q=new", "https://example.org/search?q=new"},
		{"https://example.org/search", "https://other.example/a", "https://other.example/a"},
	}
	for _, tc := range cases {
		if got := resolveURL(tc.base, tc.ref); got != tc.want {
			t.Errorf("resolveURL(%q, %q) = %q, want %q", tc.base, tc.ref, got, tc.want)
		}
	}
}
