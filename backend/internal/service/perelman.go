package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"

	"github.com/4q4r/cindex/backend/internal/domain"
)

const perelmanSystemPrompt = `You are a scientific literature analysis agent using the PERELMAN method. You receive an article as extracted TEXT. Your tasks:

1. Elicit the article's core domain contribution — its claims, findings, key definitions, and methodology.
2. Extract up to %d VERBATIM passages that best capture that contribution. Prioritize results, conclusions, and definitions. Quote text MUST be copied word-for-word from the article (you may include transcribed formulas inline). For each quote give its location (e.g. "abstract", "section 3", "page 2", "figure 1 caption") and a short rationale.
3. Transcribe mathematical formulas present in the extracted text as LaTeX — use $...$ for inline and $$...$$ for display formulas — with their location and an optional caption.
4. Convert tables and figure descriptions present in the extracted text to markdown where feasible. Mark the figure kind (figure | graph | table).
5. Write the article's TLDR: a concise 1-2 sentence summary of the core contribution and main result, in Russian (the interface language).

Return a single JSON object with exactly this shape:
{
  "tldr": "...",
  "quotes": [{"text": "...", "location": "...", "relevance": 0.0-1.0, "rationale": "..."}],
  "formulas": [{"latex": "...", "location": "...", "caption": "..."}],
  "figures": [{"markdown": "...", "location": "...", "caption": "...", "kind": "figure"}]
}

If a region is unreadable even after zooming, transcribe what you can and note the uncertainty in the caption. If the article has no extractable content, return all three lists empty and an empty string for tldr.`

// PerelmanConfig controls query-agnostic extraction prompt size.
type PerelmanConfig struct {
	MaxQuotes     int
	MaxInputChars int
}

// Formula is a formula transcribed as LaTeX.
type Formula struct {
	Latex    string `json:"latex"`
	Location string `json:"location"`
	Caption  string `json:"caption"`
}

// Figure is a graph, plot, table, or figure converted to markdown.
type Figure struct {
	Markdown string `json:"markdown"`
	Location string `json:"location"`
	Caption  string `json:"caption"`
	Kind     string `json:"kind"`
}

// ExtractionResult is the parsed PERELMAN output for one article.
type ExtractionResult struct {
	Quotes   []domain.Quote
	Formulas []Formula
	Figures  []Figure
	TLDR     string
}

// IsEmpty reports whether the extraction contains no usable output.
func (r ExtractionResult) IsEmpty() bool {
	return len(r.Quotes) == 0 && len(r.Formulas) == 0 && len(r.Figures) == 0 && r.TLDR == ""
}

// Perelman performs text-first, query-agnostic PERELMAN extraction.
type Perelman struct {
	client *LLMClient
	cfg    PerelmanConfig
}

// NewPerelman constructs a text-first PERELMAN extractor.
func NewPerelman(client *LLMClient, cfg PerelmanConfig) *Perelman {
	if cfg.MaxQuotes <= 0 {
		cfg.MaxQuotes = 3
	}
	if cfg.MaxInputChars <= 0 {
		cfg.MaxInputChars = 12000
	}
	return &Perelman{client: client, cfg: cfg}
}

// Extract sends title, abstract, and full text to the LLM without any search
// query. Transport/provider failures are returned; malformed content safely
// degrades to an empty result.
func (p *Perelman) Extract(ctx context.Context, article domain.Article) (ExtractionResult, error) {
	if p == nil || p.client == nil {
		return ExtractionResult{}, errors.New("perelman: LLM client is required")
	}
	content := capRunes(articleInput(article), p.cfg.MaxInputChars)
	messages := []ChatMessage{
		{Role: "system", Content: fmt.Sprintf(perelmanSystemPrompt, p.cfg.MaxQuotes)},
		{Role: "user", Content: content},
	}
	message, err := p.client.ChatWithExtra(ctx, messages, map[string]any{
		"response_format": map[string]string{"type": "json_object"},
	})
	var httpErr *LLMHTTPError
	if errors.As(err, &httpErr) && httpErr.StatusCode == http.StatusBadRequest {
		message, err = p.client.Chat(ctx, messages)
	}
	if err != nil {
		return ExtractionResult{}, err
	}
	contentValue, ok := message["content"].(string)
	if !ok {
		return ExtractionResult{}, nil
	}
	result := parseExtractionResult(contentValue)
	if len(result.Quotes) > p.cfg.MaxQuotes {
		result.Quotes = result.Quotes[:p.cfg.MaxQuotes]
	}
	return limitExtractionResult(result), nil
}

func articleInput(article domain.Article) string {
	return "ARTICLE TITLE:\n" + strings.TrimSpace(article.Title) +
		"\n\nARTICLE ABSTRACT:\n" + strings.TrimSpace(article.Abstract) +
		"\n\nARTICLE FULL TEXT:\n" + strings.TrimSpace(article.FullText)
}

func parseExtractionResult(content string) ExtractionResult {
	for _, candidate := range jsonCandidates(content) {
		var raw struct {
			TLDR     json.RawMessage `json:"tldr"`
			Quotes   json.RawMessage `json:"quotes"`
			Formulas json.RawMessage `json:"formulas"`
			Figures  json.RawMessage `json:"figures"`
		}
		if !decodeLenient([]byte(candidate), &raw) {
			continue
		}
		if raw.TLDR == nil && raw.Quotes == nil && raw.Formulas == nil && raw.Figures == nil {
			continue
		}
		var tldr any
		var quotes, formulas, figures []json.RawMessage
		_ = json.Unmarshal(raw.TLDR, &tldr)
		_ = json.Unmarshal(raw.Quotes, &quotes)
		_ = json.Unmarshal(raw.Formulas, &formulas)
		_ = json.Unmarshal(raw.Figures, &figures)
		return normalizeExtraction(tldr, quotes, formulas, figures)
	}
	return ExtractionResult{}
}

func normalizeExtraction(tldrValue any, quoteValues, formulaValues, figureValues []json.RawMessage) ExtractionResult {
	result := ExtractionResult{}
	if tldr, ok := tldrValue.(string); ok {
		result.TLDR = capRunes(strings.TrimSpace(tldr), 2000)
	}
	for _, value := range quoteValues {
		var item struct {
			Text      any `json:"text"`
			Location  any `json:"location"`
			Relevance any `json:"relevance"`
			Rationale any `json:"rationale"`
		}
		if !decodeLenient(value, &item) {
			continue
		}
		text := safeString(item.Text)
		if text == "" {
			continue
		}
		quote := domain.Quote{
			Text: text, Location: safeString(item.Location), Relevance: clampRelevance(item.Relevance),
			Rationale: safeString(item.Rationale),
		}
		result.Quotes = append(result.Quotes, quote)
	}
	for _, value := range formulaValues {
		var item struct {
			Latex    any `json:"latex"`
			Location any `json:"location"`
			Caption  any `json:"caption"`
		}
		if !decodeLenient(value, &item) {
			continue
		}
		latex := safeString(item.Latex)
		if latex != "" {
			result.Formulas = append(result.Formulas, Formula{
				Latex: latex, Location: safeString(item.Location), Caption: safeString(item.Caption),
			})
		}
	}
	for _, value := range figureValues {
		var item struct {
			Markdown any `json:"markdown"`
			Location any `json:"location"`
			Caption  any `json:"caption"`
			Kind     any `json:"kind"`
		}
		if !decodeLenient(value, &item) {
			continue
		}
		markdown := safeString(item.Markdown)
		if markdown == "" {
			continue
		}
		kind := safeString(item.Kind)
		if kind == "" {
			kind = "figure"
		}
		result.Figures = append(result.Figures, Figure{
			Markdown: markdown, Location: safeString(item.Location), Caption: safeString(item.Caption), Kind: kind,
		})
	}
	return result
}

func jsonCandidates(content string) []string {
	trimmed := strings.TrimSpace(content)
	if strings.HasPrefix(trimmed, "```") {
		if newline := strings.IndexByte(trimmed, '\n'); newline >= 0 {
			trimmed = trimmed[newline+1:]
		}
		if end := strings.LastIndex(trimmed, "```"); end >= 0 {
			trimmed = strings.TrimSpace(trimmed[:end])
		}
	}
	var candidates []string
	for start := 0; start < len(trimmed); {
		if trimmed[start] != '{' {
			start++
			continue
		}
		depth, inString, escaped := 0, false, false
	scanObject:
		for end := start; end < len(trimmed); end++ {
			char := trimmed[end]
			if inString {
				if escaped {
					escaped = false
				} else if char == '\\' {
					escaped = true
				} else if char == '"' {
					inString = false
				}
				continue
			}
			switch char {
			case '"':
				inString = true
			case '{':
				depth++
			case '}':
				depth--
				if depth == 0 {
					candidates = append(candidates, trimmed[start:end+1])
					start = end + 1
					break scanObject
				}
			}
		}
		if depth != 0 {
			break
		}
	}
	return candidates
}

func limitExtractionResult(result ExtractionResult) ExtractionResult {
	const maxItems = 100
	if len(result.Formulas) > maxItems {
		result.Formulas = result.Formulas[:maxItems]
	}
	if len(result.Figures) > maxItems {
		result.Figures = result.Figures[:maxItems]
	}
	for i := range result.Quotes {
		result.Quotes[i].Text = capRunes(result.Quotes[i].Text, 12000)
		result.Quotes[i].Location = capRunes(result.Quotes[i].Location, 1000)
		result.Quotes[i].Rationale = capRunes(result.Quotes[i].Rationale, 4000)
	}
	for i := range result.Formulas {
		result.Formulas[i].Latex = capRunes(result.Formulas[i].Latex, 12000)
		result.Formulas[i].Location = capRunes(result.Formulas[i].Location, 1000)
		result.Formulas[i].Caption = capRunes(result.Formulas[i].Caption, 4000)
	}
	for i := range result.Figures {
		result.Figures[i].Markdown = capRunes(result.Figures[i].Markdown, 20000)
		result.Figures[i].Location = capRunes(result.Figures[i].Location, 1000)
		result.Figures[i].Caption = capRunes(result.Figures[i].Caption, 4000)
		result.Figures[i].Kind = capRunes(result.Figures[i].Kind, 100)
	}
	return result
}

func decodeLenient(data []byte, target any) bool {
	if json.Unmarshal(data, target) == nil {
		return true
	}
	repaired := repairJSONControlChars(data)
	return json.Unmarshal(repaired, target) == nil
}

func repairJSONControlChars(data []byte) []byte {
	out := make([]byte, 0, len(data))
	inString := false
	for i := 0; i < len(data); i++ {
		char := data[i]
		if !inString {
			out = append(out, char)
			if char == '"' {
				inString = true
			}
			continue
		}
		if char == '"' {
			inString = false
			out = append(out, char)
			continue
		}
		if char == '\\' {
			out = append(out, char)
			if i+1 < len(data) {
				i++
				next := data[i]
				if next <= 0x1f {
					out = appendControlEscape(out, next)
				} else {
					out = append(out, next)
				}
			}
			continue
		}
		if char <= 0x1f {
			out = appendControlEscape(out, char)
			continue
		}
		out = append(out, char)
	}
	return out
}

func appendControlEscape(out []byte, char byte) []byte {
	switch char {
	case '\n':
		return append(out, '\\', 'n')
	case '\r':
		return append(out, '\\', 'r')
	case '\t':
		return append(out, '\\', 't')
	case '\b':
		return append(out, '\\', 'b')
	case '\f':
		return append(out, '\\', 'f')
	default:
		return append(out, fmt.Sprintf("\\u%04x", char)...)
	}
}

func safeString(value any) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	return strings.TrimSpace(text)
}

func clampRelevance(value any) float64 {
	var relevance float64
	switch typed := value.(type) {
	case float64:
		relevance = typed
	case json.Number:
		relevance, _ = typed.Float64()
	case string:
		relevance, _ = strconv.ParseFloat(strings.TrimSpace(typed), 64)
	default:
		return 0
	}
	if math.IsNaN(relevance) || relevance < 0 {
		return 0
	}
	if relevance > 1 {
		return 1
	}
	return relevance
}

func capRunes(value string, max int) string {
	if max <= 0 {
		return ""
	}
	runes := []rune(value)
	if len(runes) <= max {
		return value
	}
	return string(runes[:max])
}
