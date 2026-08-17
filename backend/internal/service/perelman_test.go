package service

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/4q4r/cindex/backend/internal/domain"
)

func TestParseExtractionResultFencedEmbeddedAndRawControls(t *testing.T) {
	t.Parallel()

	content := "Result follows (ignore {prose}):\n```json\n" +
		"{\"tldr\":\" summary \",\"quotes\":[" +
		"{\"text\":\"line one\nline two\",\"location\":\" abstract \",\"relevance\":1.7}," +
		"{\"text\":\"   \"}]," +
		"\"formulas\":[{\"latex\":\"$\\\\alpha + \\\\beta$\"},{\"latex\":null}]," +
		"\"figures\":[{\"markdown\":\"|x|y|\",\"kind\":\"\"},{\"markdown\":\"\"}]}\n```\nThanks"
	result := parseExtractionResult(content)

	if result.TLDR != "summary" || len(result.Quotes) != 1 || len(result.Formulas) != 1 || len(result.Figures) != 1 {
		t.Fatalf("result = %#v", result)
	}
	if result.Quotes[0].Text != "line one\nline two" {
		t.Fatalf("raw newline was not repaired: %q", result.Quotes[0].Text)
	}
	if got := result.Quotes[0].Relevance; got != 1 {
		t.Fatalf("relevance = %v", got)
	}
	if result.Formulas[0].Latex != `$\alpha + \beta$` {
		t.Fatalf("LaTeX corrupted: %q", result.Formulas[0].Latex)
	}
	if result.Figures[0].Kind != "figure" {
		t.Fatalf("default kind = %q", result.Figures[0].Kind)
	}
}

func TestParseExtractionResultClampAndUnicodeTLDRCap(t *testing.T) {
	t.Parallel()

	tldr := strings.Repeat("界", 2001)
	payload := fmt.Sprintf(`{"tldr":%q,"quotes":[{"text":"low","relevance":-2},{"text":"bad","relevance":"not-a-number"},{"text":"high","relevance":"2.5"}],"formulas":[],"figures":[]}`, tldr)
	result := parseExtractionResult(payload)
	if utf8.RuneCountInString(result.TLDR) != 2000 || !utf8.ValidString(result.TLDR) {
		t.Fatalf("TLDR rune cap invalid: runes=%d valid=%v", utf8.RuneCountInString(result.TLDR), utf8.ValidString(result.TLDR))
	}
	want := []float64{0, 0, 1}
	for i, quote := range result.Quotes {
		if got := quote.Relevance; got != want[i] {
			t.Errorf("quote %d relevance = %v, want %v", i, got, want[i])
		}
	}
}

func TestParseExtractionResultMalformedIsEmpty(t *testing.T) {
	t.Parallel()
	if result := parseExtractionResult(`not JSON {"tldr":`); !reflect.DeepEqual(result, ExtractionResult{}) {
		t.Fatalf("result = %#v", result)
	}
	result := parseExtractionResult(`{"tldr":"kept","quotes":{},"formulas":"wrong","figures":null}`)
	if result.TLDR != "kept" || len(result.Quotes) != 0 || len(result.Formulas) != 0 || len(result.Figures) != 0 {
		t.Fatalf("wrong member types should be discarded independently: %#v", result)
	}
}

func TestParseExtractionResultSkipsUnrelatedJSON(t *testing.T) {
	result := parseExtractionResult(`metadata {"provider":"zai"} result {"tldr":"actual","quotes":[],"formulas":[],"figures":[]}`)
	if result.TLDR != "actual" {
		t.Fatalf("result = %#v", result)
	}
}

func TestPerelmanExtractContractCapAndQueryAbsence(t *testing.T) {
	t.Parallel()

	var requestBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&requestBody); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_, _ = io.WriteString(w, `{"choices":[{"message":{"role":"assistant","content":"{\"tldr\":\"ok\",\"quotes\":[],\"formulas\":[],\"figures\":[]}"}}]}`)
	}))
	defer server.Close()

	client, err := NewLLMClient(LLMConfig{
		BaseURL: server.URL, APIKey: "secret", Model: "model", Timeout: time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	extractor := NewPerelman(client, PerelmanConfig{MaxQuotes: 4, MaxInputChars: 90})
	result, err := extractor.Extract(context.Background(), domain.Article{
		Title: "Заголовок", Abstract: "Аннотация", FullText: strings.Repeat("界", 100),
	})
	if err != nil || result.TLDR != "ok" {
		t.Fatalf("result/err = %#v/%v", result, err)
	}

	messages, ok := requestBody["messages"].([]any)
	if !ok || len(messages) != 2 {
		t.Fatalf("messages = %#v", requestBody["messages"])
	}
	responseFormat, ok := requestBody["response_format"].(map[string]any)
	if !ok || responseFormat["type"] != "json_object" {
		t.Fatalf("response_format = %#v", requestBody["response_format"])
	}
	system := messages[0].(map[string]any)["content"].(string)
	user := messages[1].(map[string]any)["content"].(string)
	if !strings.Contains(system, "Extract up to 4 VERBATIM") || !strings.Contains(system, `"relevance": 0.0-1.0`) {
		t.Fatalf("system prompt contract missing: %q", system)
	}
	if strings.Contains(strings.ToLower(system+user), "search query") || strings.Contains(string(mustJSON(t, requestBody)), `"query"`) {
		t.Fatalf("query leaked into request: %#v", requestBody)
	}
	if utf8.RuneCountInString(user) != 90 || !utf8.ValidString(user) {
		t.Fatalf("input cap invalid: runes=%d valid=%v, content=%q", utf8.RuneCountInString(user), utf8.ValidString(user), user)
	}
}

func TestPerelmanRetriesWithoutResponseFormatOnProvider400(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = io.WriteString(w, `{"error":"response_format unsupported"}`)
			return
		}
		_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"{\"tldr\":\"ok\",\"quotes\":[],\"formulas\":[],\"figures\":[]}"}}]}`)
	}))
	defer server.Close()
	client, err := NewLLMClient(LLMConfig{BaseURL: server.URL, APIKey: "secret", Model: "model"})
	if err != nil {
		t.Fatal(err)
	}
	result, err := NewPerelman(client, PerelmanConfig{}).Extract(context.Background(), domain.Article{Title: "x"})
	if err != nil || result.TLDR != "ok" || calls != 2 {
		t.Fatalf("result=%#v calls=%d err=%v", result, calls, err)
	}
}

func TestPerelmanExtractMalformedContentAndProviderError(t *testing.T) {
	t.Parallel()

	t.Run("malformed content is empty without error", func(t *testing.T) {
		extractor, closeServer := testPerelmanServer(t, http.StatusOK, `{"choices":[{"message":{"content":"not-json"}}]}`)
		defer closeServer()
		result, err := extractor.Extract(context.Background(), domain.Article{Title: "x"})
		if err != nil || !reflect.DeepEqual(result, ExtractionResult{}) {
			t.Fatalf("result/err = %#v/%v", result, err)
		}
	})

	t.Run("provider error is returned", func(t *testing.T) {
		extractor, closeServer := testPerelmanServer(t, http.StatusBadGateway, `{"error":"down"}`)
		defer closeServer()
		_, err := extractor.Extract(context.Background(), domain.Article{Title: "x"})
		if err == nil {
			t.Fatal("expected provider error")
		}
	})
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func testPerelmanServer(t *testing.T, status int, body string) (*Perelman, func()) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	client, err := NewLLMClient(LLMConfig{
		BaseURL: server.URL, APIKey: "secret", Model: "model", Timeout: time.Second,
	})
	if err != nil {
		server.Close()
		t.Fatal(err)
	}
	return NewPerelman(client, PerelmanConfig{}), server.Close
}
