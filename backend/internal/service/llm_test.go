package service

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestLLMClientRequestContractAndFullMessage(t *testing.T) {
	t.Parallel()

	var gotPath, gotAuth string
	var gotBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		if r.Method != http.MethodPost {
			t.Errorf("method = %s", r.Method)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Errorf("content type = %q", got)
		}
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("decode body: %v", err)
		}
		_, _ = io.WriteString(w, `{"choices":[{"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"zoom","arguments":"{}"}}],"provider_field":"kept"}}]}`)
	}))
	defer server.Close()

	client, err := NewLLMClient(LLMConfig{
		BaseURL: server.URL + "/", APIKey: "secret", Model: "model-x",
		Timeout: time.Second, Temperature: 0.25,
		ExtraBody: map[string]any{"thinking": map[string]any{"type": "disabled"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	message, err := client.Chat(context.Background(), []ChatMessage{{Role: "user", Content: "hello"}})
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/chat/completions" || gotAuth != "Bearer secret" {
		t.Fatalf("path/auth = %q/%q", gotPath, gotAuth)
	}
	if gotBody["model"] != "model-x" || gotBody["temperature"] != 0.25 {
		t.Fatalf("body = %#v", gotBody)
	}
	if _, ok := gotBody["thinking"].(map[string]any); !ok {
		t.Fatalf("extra body missing: %#v", gotBody)
	}
	toolCalls, ok := message["tool_calls"].([]any)
	if !ok || len(toolCalls) != 1 || message["provider_field"] != "kept" {
		t.Fatalf("full assistant message not preserved: %#v", message)
	}
}

func TestLLMClientRetriesNetworkFailuresWithExpectedDelays(t *testing.T) {
	t.Parallel()

	transport := &sequenceTransport{failures: 2}
	client, err := NewLLMClient(LLMConfig{
		BaseURL: "https://example.invalid/v1", APIKey: "secret", Model: "model",
		Timeout: time.Second, HTTPClient: &http.Client{Transport: transport},
	})
	if err != nil {
		t.Fatal(err)
	}
	var delays []time.Duration
	client.sleep = func(_ context.Context, delay time.Duration) error {
		delays = append(delays, delay)
		return nil
	}

	if _, err := client.Chat(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if transport.calls != 3 {
		t.Fatalf("calls = %d, want 3", transport.calls)
	}
	want := []time.Duration{600 * time.Millisecond, 1200 * time.Millisecond}
	if len(delays) != len(want) || delays[0] != want[0] || delays[1] != want[1] {
		t.Fatalf("delays = %v, want %v", delays, want)
	}
}

func TestLLMClientZAI429Policy(t *testing.T) {
	t.Parallel()

	t.Run("retry-after", func(t *testing.T) {
		transport := &rateLimitTransport{code: "1302", retryAfter: "1.5"}
		client := mustTestLLMClient(t, transport)
		var delays []time.Duration
		client.sleep = func(_ context.Context, delay time.Duration) error {
			delays = append(delays, delay)
			return nil
		}
		if _, err := client.Chat(context.Background(), nil); err != nil {
			t.Fatal(err)
		}
		if len(delays) != 1 || delays[0] != 1500*time.Millisecond {
			t.Fatalf("delays = %v", delays)
		}
	})

	t.Run("terminal-quota", func(t *testing.T) {
		transport := &rateLimitTransport{code: "1308"}
		client := mustTestLLMClient(t, transport)
		client.sleep = func(_ context.Context, _ time.Duration) error {
			t.Fatal("terminal quota response must not sleep or retry")
			return nil
		}
		_, err := client.Chat(context.Background(), nil)
		var rateErr *LLMRateLimitError
		if !errors.As(err, &rateErr) || !rateErr.Terminal || transport.calls != 1 {
			t.Fatalf("err/calls = %#v/%d", err, transport.calls)
		}
	})
}

func TestLLMClientSharedRequestStartInterval(t *testing.T) {
	transport := &sequenceTransport{}
	client, err := NewLLMClient(LLMConfig{
		BaseURL: "https://example.invalid", APIKey: "secret", Model: "model",
		RequestInterval: time.Second, HTTPClient: &http.Client{Transport: transport},
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(100, 0)
	client.now = func() time.Time { return now }
	var mu sync.Mutex
	var delays []time.Duration
	client.sleep = func(_ context.Context, delay time.Duration) error {
		mu.Lock()
		defer mu.Unlock()
		delays = append(delays, delay)
		now = now.Add(delay)
		return nil
	}
	if _, err := client.Chat(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Chat(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if len(delays) != 1 || delays[0] != time.Second {
		t.Fatalf("delays = %v", delays)
	}
}

type sequenceTransport struct {
	calls    int
	failures int
}

func (t *sequenceTransport) RoundTrip(*http.Request) (*http.Response, error) {
	t.calls++
	if t.calls <= t.failures {
		return nil, errors.New("temporary network failure")
	}
	return jsonResponse(http.StatusOK, `{"choices":[{"message":{"role":"assistant","content":"{}"}}]}`, nil), nil
}

type rateLimitTransport struct {
	calls      int
	code       string
	retryAfter string
}

func (t *rateLimitTransport) RoundTrip(*http.Request) (*http.Response, error) {
	t.calls++
	if t.calls == 1 {
		headers := http.Header{}
		if t.retryAfter != "" {
			headers.Set("Retry-After", t.retryAfter)
		}
		return jsonResponse(http.StatusTooManyRequests, `{"error":{"code":"`+t.code+`"}}`, headers), nil
	}
	return jsonResponse(http.StatusOK, `{"choices":[{"message":{"content":"{}"}}]}`, nil), nil
}

func mustTestLLMClient(t *testing.T, transport http.RoundTripper) *LLMClient {
	t.Helper()
	client, err := NewLLMClient(LLMConfig{
		BaseURL: "https://example.invalid", APIKey: "secret", Model: "model",
		HTTPClient: &http.Client{Transport: transport},
	})
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func jsonResponse(status int, body string, headers http.Header) *http.Response {
	if headers == nil {
		headers = http.Header{}
	}
	return &http.Response{
		StatusCode: status,
		Header:     headers,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}
