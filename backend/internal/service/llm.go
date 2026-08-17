package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	llmMaxAttempts         = 3
	llmMaxOverloadAttempts = 5
	llmMaxResponseBytes    = 8 << 20
)

var zaiTerminalCodes = map[string]struct{}{
	"1304": {},
	"1308": {},
	"1309": {},
	"1310": {},
}

// LLMConfig configures an OpenAI-compatible chat-completions client.
type LLMConfig struct {
	BaseURL         string
	APIKey          string
	Model           string
	Timeout         time.Duration
	Temperature     float64
	ExtraBody       map[string]any
	RequestInterval time.Duration
	HTTPClient      *http.Client
}

// ChatMessage is an OpenAI-compatible conversation message. Content is kept
// polymorphic so callers can use either text or multimodal content parts.
type ChatMessage struct {
	Role       string `json:"role"`
	Content    any    `json:"content,omitempty"`
	ToolCalls  any    `json:"tool_calls,omitempty"`
	ToolCallID string `json:"tool_call_id,omitempty"`
}

// AssistantMessage preserves the complete assistant message, including
// provider extensions and OpenAI-compatible tool_calls.
type AssistantMessage map[string]any

// LLMHTTPError reports a terminal non-success response from the provider.
type LLMHTTPError struct {
	StatusCode int
	Body       string
}

func (e *LLMHTTPError) Error() string {
	return fmt.Sprintf("llm: HTTP %d: %s", e.StatusCode, e.Body)
}

// LLMRateLimitError reports an HTTP 429. Terminal is true for Z.AI quota,
// usage-window, expired-plan, and insufficient-balance codes.
type LLMRateLimitError struct {
	Code          string
	RetryAfter    time.Duration
	HasRetryAfter bool
	Terminal      bool
	Body          string
}

func (e *LLMRateLimitError) Error() string {
	return "llm: HTTP 429: " + e.Body
}

type httpDoer interface {
	Do(*http.Request) (*http.Response, error)
}

// LLMClient is a concurrency-safe OpenAI-compatible client.
type LLMClient struct {
	cfg    LLMConfig
	client httpDoer

	rateGate  chan struct{}
	lastStart time.Time

	now    func() time.Time
	sleep  func(context.Context, time.Duration) error
	jitter func(time.Duration) time.Duration
}

// NewLLMClient validates cfg and constructs an OpenAI-compatible client.
func NewLLMClient(cfg LLMConfig) (*LLMClient, error) {
	if strings.TrimSpace(cfg.BaseURL) == "" {
		return nil, errors.New("llm: base URL is required")
	}
	if strings.TrimSpace(cfg.APIKey) == "" {
		return nil, errors.New("llm: API key is required")
	}
	if strings.TrimSpace(cfg.Model) == "" {
		return nil, errors.New("llm: model is required")
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 120 * time.Second
	}
	if cfg.RequestInterval < 0 {
		cfg.RequestInterval = 0
	}
	client := cfg.HTTPClient
	if client == nil {
		client = &http.Client{}
	}
	return &LLMClient{
		cfg:      cfg,
		client:   client,
		rateGate: make(chan struct{}, 1),
		now:      time.Now,
		sleep:    sleepContext,
		jitter:   jitterDuration,
	}, nil
}

// Chat sends one chat-completions request and returns the complete first
// assistant message. ExtraBody is merged last for provider-specific fields.
func (c *LLMClient) Chat(ctx context.Context, messages []ChatMessage) (AssistantMessage, error) {
	return c.ChatWithExtra(ctx, messages, nil)
}

// ChatWithExtra sends a chat request with per-call body overrides merged last.
func (c *LLMClient) ChatWithExtra(ctx context.Context, messages []ChatMessage, extra map[string]any) (AssistantMessage, error) {
	body := make(map[string]any, len(c.cfg.ExtraBody)+len(extra)+3)
	body["model"] = c.cfg.Model
	body["messages"] = messages
	body["temperature"] = c.cfg.Temperature
	for key, value := range c.cfg.ExtraBody {
		body[key] = value
	}
	for key, value := range extra {
		body[key] = value
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("llm: encode request: %w", err)
	}
	endpoint := strings.TrimRight(c.cfg.BaseURL, "/") + "/chat/completions"
	for attempt := 1; ; attempt++ {
		if err := c.waitForRequestStart(ctx); err != nil {
			return nil, err
		}
		message, retryable, err := c.post(ctx, endpoint, payload)
		if err == nil {
			return message, nil
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}

		var rateErr *LLMRateLimitError
		if errors.As(err, &rateErr) {
			delay := rateErr.RetryAfter
			if rateErr.Terminal {
				return nil, err
			}
			maxAttempts := llmMaxAttempts
			if rateErr.Code == "1305" {
				maxAttempts = llmMaxOverloadAttempts
			}
			if attempt >= maxAttempts {
				return nil, err
			}
			if !rateErr.HasRetryAfter {
				base := 2 * time.Second
				if rateErr.Code == "1305" {
					base = 4 * time.Second
				}
				delay = base << (attempt - 1)
				if delay > 30*time.Second {
					delay = 30 * time.Second
				}
				delay = c.jitter(delay)
			}
			if err := c.sleep(ctx, delay); err != nil {
				return nil, err
			}
		} else {
			if !retryable || attempt >= llmMaxAttempts {
				return nil, err
			}
			delay := time.Duration(attempt) * 600 * time.Millisecond
			if err := c.sleep(ctx, delay); err != nil {
				return nil, err
			}
		}
	}
}

func (c *LLMClient) post(ctx context.Context, endpoint string, payload []byte) (AssistantMessage, bool, error) {
	attemptCtx, cancel := context.WithTimeout(ctx, c.cfg.Timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, false, fmt.Errorf("llm: build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.cfg.APIKey)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, true, fmt.Errorf("llm: request: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, llmMaxResponseBytes+1))
	if err != nil {
		return nil, true, fmt.Errorf("llm: read response: %w", err)
	}
	if len(data) > llmMaxResponseBytes {
		return nil, false, errors.New("llm: response exceeds size limit")
	}
	preview := string(data)
	if len(preview) > 200 {
		preview = preview[:200]
	}
	if resp.StatusCode == http.StatusTooManyRequests {
		code := parseZAIErrorCode(data)
		_, terminal := zaiTerminalCodes[code]
		retryAfter, hasRetryAfter := parseRetryAfter(resp.Header.Get("Retry-After"))
		return nil, false, &LLMRateLimitError{
			Code: code, RetryAfter: retryAfter, HasRetryAfter: hasRetryAfter,
			Terminal: terminal, Body: preview,
		}
	}
	if resp.StatusCode >= http.StatusBadRequest {
		return nil, false, &LLMHTTPError{StatusCode: resp.StatusCode, Body: preview}
	}

	var decoded struct {
		Choices []struct {
			Message AssistantMessage `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(data, &decoded); err != nil {
		return nil, false, fmt.Errorf("llm: invalid JSON response: %w", err)
	}
	if len(decoded.Choices) == 0 || decoded.Choices[0].Message == nil {
		return nil, false, errors.New("llm: response missing assistant message")
	}
	return decoded.Choices[0].Message, false, nil
}

func (c *LLMClient) waitForRequestStart(ctx context.Context) error {
	if c.cfg.RequestInterval <= 0 {
		return nil
	}
	select {
	case c.rateGate <- struct{}{}:
		defer func() { <-c.rateGate }()
	case <-ctx.Done():
		return ctx.Err()
	}
	if !c.lastStart.IsZero() {
		wait := c.cfg.RequestInterval - c.now().Sub(c.lastStart)
		if wait > 0 {
			if err := c.sleep(ctx, wait); err != nil {
				return err
			}
		}
	}
	c.lastStart = c.now()
	return nil
}

func parseRetryAfter(raw string) (time.Duration, bool) {
	seconds, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil || seconds < 0 {
		return 0, false
	}
	return time.Duration(seconds * float64(time.Second)), true
}

func parseZAIErrorCode(data []byte) string {
	var payload struct {
		Error struct {
			Code any `json:"code"`
		} `json:"error"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&payload); err != nil || payload.Error.Code == nil {
		return ""
	}
	return fmt.Sprint(payload.Error.Code)
}

func sleepContext(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func jitterDuration(delay time.Duration) time.Duration {
	if delay <= 0 {
		return 0
	}
	factor := 0.75 + rand.Float64()*0.5 //nolint:gosec // retry jitter is not security-sensitive
	return time.Duration(float64(delay) * factor)
}
