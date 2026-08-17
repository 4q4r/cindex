// Package service implements application services with Django parity.
package service

import (
	"container/list"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// SupportedLanguages mirrors apps.core.translate.SUPPORTED_LANGUAGES.
var SupportedLanguages = []string{"en", "ru", "de", "fr", "es", "pt", "zh-CN", "ja", "ko"}

// ExaSearchLanguages mirrors apps.core.translate.EXA_SEARCH_LANGUAGES.
var ExaSearchLanguages = []string{"en", "ru", "de", "fr", "es", "zh-CN", "ja", "ko"}

// SourceLanguages mirrors apps.core.translate.SOURCE_LANGUAGES.
var SourceLanguages = map[string]string{
	"europe_pmc": "en", "openalex": "en", "crossref": "en", "pubmed": "en",
	"arxiv": "en", "doaj": "en", "pmc": "en", "core": "en", "biorxiv": "en",
	"medrxiv": "en", "dblp": "en", "hal": "fr", "zenodo": "en", "iacr": "en",
	"exa":   "multi",
	"cinii": "ja", "coaj": "zh-CN", "sciengine": "zh-CN", "sciopen": "zh-CN",
	"cyberleninka": "ru", "mathnet": "ru", "scielo": "es", "redalyc": "es",
	"korea_science": "ko", "persee": "fr", "open_edition": "fr",
	"revistas_csic": "es", "medknow": "en", "dergipark": "en", "hrcak": "en",
	"ajol": "en",
}

const translateCacheSize = 512

// Translate provides cross-lingual query translation with GoogleTranslator
// (no key) and MyMemory fallback, LRU-cached. Parity with
// apps.core.translate.
type Translate struct {
	client *http.Client
	cache  *lruCache
}

// NewTranslate builds the translation service.
func NewTranslate() *Translate {
	return &Translate{
		client: &http.Client{Timeout: 15 * time.Second},
		cache:  newLRUCache(translateCacheSize),
	}
}

// DetectLanguage classifies a short query by script (parity with
// apps.core.translate._detect_language).
func DetectLanguage(text string) string {
	for _, r := range text {
		if r >= 0x0400 && r <= 0x04FF {
			return "ru"
		}
	}
	for _, r := range text {
		switch {
		case r >= 0x4E00 && r <= 0x9FFF:
			return "zh-CN"
		case (r >= 0x3040 && r <= 0x309F) || (r >= 0x31F0 && r <= 0x31FF):
			return "ja"
		case r >= 0xAC00 && r <= 0xD7AF:
			return "ko"
		}
	}
	return "en"
}

// TranslateQuery translates a short query into targetLang, falling back to
// MyMemory when Google fails and to the original when both fail. Results are
// cached (parity with apps.core.translate.translate_query).
func (t *Translate) TranslateQuery(ctx context.Context, query, targetLang string) string {
	if strings.TrimSpace(query) == "" {
		return query
	}
	source := DetectLanguage(query)
	if source == targetLang {
		return query
	}
	cacheKey := query + "\x00" + targetLang
	if cached, ok := t.cache.get(cacheKey); ok {
		return cached
	}
	queryText := strings.TrimSpace(query)
	if len(queryText) > 500 {
		queryText = queryText[:500]
	}

	if result, err := t.google(ctx, queryText, targetLang); err == nil && strings.TrimSpace(result) != "" {
		result = strings.TrimSpace(result)
		t.cache.put(cacheKey, result)
		return result
	}
	if result, err := t.myMemory(ctx, queryText, targetLang); err == nil && strings.TrimSpace(result) != "" {
		result = strings.TrimSpace(result)
		t.cache.put(cacheKey, result)
		return result
	}
	t.cache.put(cacheKey, query)
	return query
}

func (t *Translate) google(ctx context.Context, query, targetLang string) (string, error) {
	endpoint := "https://translate.googleapis.com/translate_a/single"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return "", err
	}
	params := url.Values{}
	params.Set("client", "gtx")
	params.Set("sl", "auto")
	params.Set("tl", targetLang)
	params.Set("dt", "t")
	params.Set("q", query)
	req.URL.RawQuery = params.Encode()

	resp, err := t.client.Do(req)
	if err != nil {
		return "", err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("google translate status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", err
	}
	var segments [][]any
	if err := json.Unmarshal(body, &segments); err != nil {
		return "", err
	}
	var parts []string
	for _, seg := range segments {
		if len(seg) > 0 {
			if s, ok := seg[0].(string); ok {
				parts = append(parts, s)
			}
		}
	}
	return strings.Join(parts, ""), nil
}

func (t *Translate) myMemory(ctx context.Context, query, targetLang string) (string, error) {
	endpoint := "https://api.mymemory.translated.net/get"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return "", err
	}
	params := url.Values{}
	params.Set("q", query)
	params.Set("langpair", "auto|"+targetLang)
	req.URL.RawQuery = params.Encode()

	resp, err := t.client.Do(req)
	if err != nil {
		return "", err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("mymemory status %d", resp.StatusCode)
	}
	var payload struct {
		ResponseData struct {
			TranslatedText string `json:"translatedText"`
		} `json:"responseData"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&payload); err != nil {
		return "", err
	}
	return payload.ResponseData.TranslatedText, nil
}

// GetSourceQueryLanguage returns the primary query language for a source key.
func GetSourceQueryLanguage(sourceKey string) string {
	if lang, ok := SourceLanguages[sourceKey]; ok {
		return lang
	}
	return "en"
}

// TranslateQueryForSource translates the query into the source's language.
func (t *Translate) TranslateQueryForSource(ctx context.Context, query, sourceKey string) string {
	target := GetSourceQueryLanguage(sourceKey)
	if target == "multi" {
		return query
	}
	return t.TranslateQuery(ctx, query, target)
}

// ExpandQueryForExa translates the query into every Exa target language.
func (t *Translate) ExpandQueryForExa(ctx context.Context, query string) map[string]string {
	results := make(map[string]string, len(ExaSearchLanguages))
	for _, lang := range ExaSearchLanguages {
		if translated := t.TranslateQuery(ctx, query, lang); translated != "" {
			results[lang] = translated
		}
	}
	return results
}

// ExpandSearchTerms returns deduplicated cross-lingual translations of query,
// excluding the original. Parity with apps.core.translate.expand_search_terms.
func (t *Translate) ExpandSearchTerms(ctx context.Context, query string) []string {
	seen := map[string]bool{strings.TrimSpace(query): true}
	var terms []string
	for _, lang := range []string{"en", "ru", "de", "fr", "es"} {
		translated := t.TranslateQuery(ctx, query, lang)
		normalized := strings.TrimSpace(translated)
		key := strings.ToLower(normalized)
		if normalized != "" && !seen[key] {
			terms = append(terms, normalized)
			seen[key] = true
		}
	}
	return terms
}

type lruEntry struct {
	key   string
	value string
}

// lruCache is a small thread-safe LRU string cache.
type lruCache struct {
	mu      sync.Mutex
	max     int
	entries map[string]*list.Element
	order   *list.List
}

func newLRUCache(max int) *lruCache {
	return &lruCache{
		max:     max,
		entries: make(map[string]*list.Element, max),
		order:   list.New(),
	}
}

func (c *lruCache) get(key string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.entries[key]; ok {
		c.order.MoveToFront(el)
		if entry, ok2 := el.Value.(*lruEntry); ok2 {
			return entry.value, true
		}
	}
	return "", false
}

func (c *lruCache) put(key, value string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.entries[key]; ok {
		if entry, ok2 := el.Value.(*lruEntry); ok2 {
			entry.value = value
		}
		c.order.MoveToFront(el)
		return
	}
	el := c.order.PushFront(&lruEntry{key: key, value: value})
	c.entries[key] = el
	if c.order.Len() > c.max {
		oldest := c.order.Back()
		if oldest != nil {
			c.order.Remove(oldest)
			if entry, ok2 := oldest.Value.(*lruEntry); ok2 {
				delete(c.entries, entry.key)
			}
		}
	}
}
