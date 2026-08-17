package service

import (
	"html"
	"strings"
	"unicode"
)

// NormalizeScholarlyText mirrors apps.core.text.normalize_scholarly_text:
// unescape, drop control characters, drop HTML tags (<[^>]+>), collapse
// whitespace, then optionally truncate.
func NormalizeScholarlyText(value string, maxLength int) string {
	text := strings.TrimSpace(html.UnescapeString(value))
	if text == "" {
		return ""
	}
	var b strings.Builder
	b.Grow(len(text))
	inTag := false
	for _, r := range text {
		if inTag {
			if r == '>' {
				inTag = false
				b.WriteByte(' ')
			}
			continue
		}
		if r == '<' {
			inTag = true
			continue
		}
		if (r < 0x20 && r != '\t' && r != '\n' && r != '\r') || r == 0x7f {
			b.WriteByte(' ')
			continue
		}
		b.WriteRune(r)
	}
	text = strings.Join(strings.Fields(b.String()), " ")
	if maxLength >= 0 && len(text) > maxLength {
		text = strings.TrimRight(text[:maxLength], " \t\n")
	}
	return text
}

// CanonicalTextKey mirrors apps.core.text.canonical_text_key: lowercase and
// replace non-word runs with a single space.
func CanonicalTextKey(value string) string {
	text := NormalizeScholarlyText(value, -1)
	if text == "" {
		return ""
	}
	text = strings.ToLower(text)
	var b strings.Builder
	b.Grow(len(text))
	for _, r := range text {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_' {
			b.WriteRune(r)
		} else {
			b.WriteByte(' ')
		}
	}
	return strings.Join(strings.Fields(b.String()), " ")
}

// NormalizeDOI mirrors apps.core.text.normalize_doi.
func NormalizeDOI(value string) string {
	return strings.ToLower(NormalizeScholarlyText(value, -1))
}
