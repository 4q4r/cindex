package connector

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	requestTimeoutSeconds = 25
	maxAttempts           = 3
	browserHTTPMargin     = 10 * time.Second
)

// Transport performs retrying HTTP requests (parity with the Django
// AsyncApiConnector retry policy: 3 attempts, linear backoff 0.6*attempt,
// terminal on HTTP >= 400 or invalid JSON, timeout 25s).
type Transport struct {
	Client   *http.Client
	MaxRetry int
}

// NewTransport builds the default retrying transport.
func NewTransport() *Transport {
	return &Transport{
		Client:   &http.Client{Timeout: requestTimeoutSeconds * time.Second},
		MaxRetry: maxAttempts,
	}
}

// apiURL returns the profile SearchURL for the source key.
func (t *Transport) apiURL(key string) string {
	p, err := ProfileFor(key)
	if err != nil {
		return ""
	}
	return p.SearchURL
}

// GetJSON fetches url with retries and decodes JSON. Returns terminal
// FetchError or transient RetryableError (after exhausting retries).
func (t *Transport) GetJSON(ctx context.Context, sourceKey, u string, headers map[string]string, out any) error {
	var lastErr error
	for attempt := 1; attempt <= t.MaxRetry; attempt++ {
		err := t.getOnce(ctx, sourceKey, u, headers, out)
		if err == nil {
			return nil
		}
		var fetchErr *FetchError
		if errors.As(err, &fetchErr) {
			return err
		}
		lastErr = err
		if attempt < t.MaxRetry {
			select {
			case <-time.After(time.Duration(attempt) * 600 * time.Millisecond):
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
	return lastErr
}

func (t *Transport) getOnce(ctx context.Context, sourceKey, u string, headers map[string]string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return retryErr(sourceKey, "build request: %v", err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := t.Client.Do(req)
	if err != nil {
		return retryErr(sourceKey, "request failed: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return retryErr(sourceKey, "read body: %v", err)
	}
	if resp.StatusCode >= 400 {
		return fetchErr(sourceKey, "HTTP %d", resp.StatusCode)
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(body, out); err != nil {
		return fetchErr(sourceKey, "invalid JSON: %v", err)
	}
	return nil
}

// PostJSON performs a retrying POST with a JSON body (Exa, CyberLeninka).
func (t *Transport) PostJSON(ctx context.Context, sourceKey, u string, headers map[string]string, payload, out any) error {
	var lastErr error
	for attempt := 1; attempt <= t.MaxRetry; attempt++ {
		err := t.postOnce(ctx, sourceKey, u, headers, payload, out)
		if err == nil {
			return nil
		}
		var fetchErr *FetchError
		if errors.As(err, &fetchErr) {
			return err
		}
		lastErr = err
		if attempt < t.MaxRetry {
			select {
			case <-time.After(time.Duration(attempt) * 600 * time.Millisecond):
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
	return lastErr
}

func (t *Transport) postOnce(ctx context.Context, sourceKey, u string, headers map[string]string, payload, out any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return fetchErr(sourceKey, "marshal payload: %v", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return retryErr(sourceKey, "build request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := t.Client.Do(req)
	if err != nil {
		return retryErr(sourceKey, "request failed: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return retryErr(sourceKey, "read body: %v", err)
	}
	if resp.StatusCode >= 400 {
		return fetchErr(sourceKey, "HTTP %d", resp.StatusCode)
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(respBody, out); err != nil {
		return fetchErr(sourceKey, "invalid JSON: %v", err)
	}
	return nil
}

// PostForm performs a retrying application/x-www-form-urlencoded POST
// (SciEngine, MathNet).
func (t *Transport) PostForm(ctx context.Context, sourceKey, u string, form url.Values, accept string, out any) error {
	var lastErr error
	for attempt := 1; attempt <= t.MaxRetry; attempt++ {
		err := t.postFormOnce(ctx, sourceKey, u, form, accept, out)
		if err == nil {
			return nil
		}
		var fetchErr *FetchError
		if errors.As(err, &fetchErr) {
			return err
		}
		lastErr = err
		if attempt < t.MaxRetry {
			select {
			case <-time.After(time.Duration(attempt) * 600 * time.Millisecond):
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
	return lastErr
}

func (t *Transport) postFormOnce(ctx context.Context, sourceKey, u string, form url.Values, accept string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, strings.NewReader(form.Encode()))
	if err != nil {
		return retryErr(sourceKey, "build request: %v", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if accept != "" {
		req.Header.Set("Accept", accept)
	}
	resp, err := t.Client.Do(req)
	if err != nil {
		return retryErr(sourceKey, "request failed: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return retryErr(sourceKey, "read body: %v", err)
	}
	if resp.StatusCode >= 400 {
		return fetchErr(sourceKey, "HTTP %d", resp.StatusCode)
	}
	if out == nil {
		return nil
	}
	switch o := out.(type) {
	case *[]byte:
		*o = body
	default:
		if err := json.Unmarshal(body, out); err != nil {
			return fetchErr(sourceKey, "invalid JSON: %v", err)
		}
	}
	return nil
}

// GetText fetches a plain-text body (IACR RSS, RSS feeds) with retries.
func (t *Transport) GetText(ctx context.Context, sourceKey, u string, accept string) (string, error) {
	var lastErr error
	for attempt := 1; attempt <= t.MaxRetry; attempt++ {
		body, err := t.getTextOnce(ctx, sourceKey, u, accept)
		if err == nil {
			return body, nil
		}
		var fetchErr *FetchError
		if errors.As(err, &fetchErr) {
			return "", err
		}
		lastErr = err
		if attempt < t.MaxRetry {
			select {
			case <-time.After(time.Duration(attempt) * 600 * time.Millisecond):
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}
	}
	return "", lastErr
}

func (t *Transport) getTextOnce(ctx context.Context, sourceKey, u, accept string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return "", retryErr(sourceKey, "build request: %v", err)
	}
	if accept != "" {
		req.Header.Set("Accept", accept)
	}
	resp, err := t.Client.Do(req)
	if err != nil {
		return "", retryErr(sourceKey, "request failed: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return "", retryErr(sourceKey, "read body: %v", err)
	}
	if resp.StatusCode >= 400 {
		return "", fetchErr(sourceKey, "HTTP %d", resp.StatusCode)
	}
	return string(body), nil
}

// Page is the decoded sidecar response body.
type Page struct {
	Body        string
	ContentType string
}

// BrowserTransport talks to the browser sidecar for HTML-mode connectors
// (parity with BrowserTransport in apps.ingestion.connectors.base).
type BrowserTransport struct {
	BaseURL  string
	Client   *http.Client
	Attempts int
}

// NewBrowserTransport builds a sidecar transport from CINDEX_BROWSER_URL
// (default http://browser:8081).
func NewBrowserTransport(baseURL string) *BrowserTransport {
	if baseURL == "" {
		baseURL = "http://browser:8081"
	}
	return &BrowserTransport{BaseURL: baseURL, Client: &http.Client{Timeout: 60 * time.Second}, Attempts: maxAttempts}
}

type fetchRequest struct {
	URL     string            `json:"url"`
	Method  string            `json:"method"`
	Timeout float64           `json:"timeout,omitempty"`
	Params  map[string]string `json:"params,omitempty"`
	Data    map[string]string `json:"data,omitempty"`
	JSON    any               `json:"json,omitempty"`
	Accept  string            `json:"accept,omitempty"`
}

type fetchResponse struct {
	Status      int    `json:"status"`
	Body        string `json:"body"`
	ContentType string `json:"content_type"`
	Encoding    string `json:"encoding"`
}

type pdfTextRequest struct {
	Body        string `json:"body"`
	OCRLanguage string `json:"ocr_language"`
}

type pdfTextResponse struct {
	Text string `json:"text"`
}

// Fetch GETs a URL through the sidecar with retries and backoff (0.6*attempt),
// no retries for upstream >= 400.
func (b *BrowserTransport) Fetch(ctx context.Context, sourceKey, u string, params map[string]string, accept string, timeoutSeconds float64) (*Page, error) {
	return b.doFetch(ctx, sourceKey, fetchRequest{URL: u, Method: "GET", Params: params, Accept: accept, Timeout: timeoutSeconds})
}

// PostForm POSTs form-urlencoded data through the sidecar.
func (b *BrowserTransport) PostForm(ctx context.Context, sourceKey, u string, data map[string]string, accept string) (*Page, error) {
	return b.doFetch(ctx, sourceKey, fetchRequest{URL: u, Method: "POST", Data: data, Accept: accept})
}

// PostJSON POSTs a JSON body through the sidecar.
func (b *BrowserTransport) PostJSON(ctx context.Context, sourceKey, u string, body any) (*Page, error) {
	return b.doFetch(ctx, sourceKey, fetchRequest{URL: u, Method: "POST", JSON: body})
}

// PDFText extracts native/OCR text from PDF bytes through the sidecar's
// /pdf-text endpoint. Network and gateway failures use the same retry policy
// as /fetch; contract and validation errors are terminal.
func (b *BrowserTransport) PDFText(ctx context.Context, sourceKey string, body []byte, ocrLanguage string) (string, error) {
	payload, err := json.Marshal(pdfTextRequest{
		Body: base64.StdEncoding.EncodeToString(body), OCRLanguage: ocrLanguage,
	})
	if err != nil {
		return "", retryErr(sourceKey, "marshal PDF text request: %v", err)
	}
	var lastErr error
	for attempt := 1; attempt <= b.Attempts; attempt++ {
		text, retry, err := b.pdfTextOnce(ctx, sourceKey, payload)
		if err == nil {
			return text, nil
		}
		if !retry {
			return "", err
		}
		lastErr = err
		if attempt < b.Attempts {
			select {
			case <-time.After(time.Duration(attempt) * 600 * time.Millisecond):
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}
	}
	return "", lastErr
}

func (b *BrowserTransport) pdfTextOnce(ctx context.Context, sourceKey string, payload []byte) (string, bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.BaseURL+"/pdf-text", bytes.NewReader(payload))
	if err != nil {
		return "", true, retryErr(sourceKey, "build PDF text request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := b.Client.Do(req)
	if err != nil {
		return "", true, retryErr(sourceKey, "PDF text request failed: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		return "", true, retryErr(sourceKey, "read PDF text response: %v", err)
	}
	if resp.StatusCode == http.StatusBadGateway || resp.StatusCode == http.StatusGatewayTimeout {
		return "", true, retryErr(sourceKey, "PDF sidecar HTTP %d", resp.StatusCode)
	}
	if resp.StatusCode >= http.StatusBadRequest {
		return "", false, fetchErr(sourceKey, "PDF sidecar HTTP %d", resp.StatusCode)
	}
	var decoded pdfTextResponse
	if err := json.Unmarshal(data, &decoded); err != nil {
		return "", false, fetchErr(sourceKey, "invalid PDF sidecar JSON: %v", err)
	}
	return NormalizeScholarly(decoded.Text, -1), false, nil
}

func (b *BrowserTransport) doFetch(ctx context.Context, sourceKey string, req fetchRequest) (*Page, error) {
	var lastErr error
	for attempt := 1; attempt <= b.Attempts; attempt++ {
		page, err := b.fetchOnce(ctx, sourceKey, req)
		if err == nil {
			return page, nil
		}
		var fetchErr *FetchError
		if errors.As(err, &fetchErr) {
			return nil, err
		}
		lastErr = err
		if attempt < b.Attempts {
			select {
			case <-time.After(time.Duration(attempt) * 600 * time.Millisecond):
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
	}
	return nil, lastErr
}

func (b *BrowserTransport) fetchOnce(ctx context.Context, sourceKey string, req fetchRequest) (*Page, error) {
	payload, err := json.Marshal(req)
	if err != nil {
		return nil, retryErr(sourceKey, "marshal sidecar request: %v", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, b.BaseURL+"/fetch", bytes.NewReader(payload))
	if err != nil {
		return nil, retryErr(sourceKey, "build sidecar request: %v", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := b.Client.Do(httpReq)
	if err != nil {
		return nil, retryErr(sourceKey, "sidecar request failed: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		return nil, retryErr(sourceKey, "read sidecar body: %v", err)
	}
	if resp.StatusCode == http.StatusBadGateway || resp.StatusCode == http.StatusGatewayTimeout {
		return nil, retryErr(sourceKey, "sidecar HTTP %d", resp.StatusCode)
	}
	if resp.StatusCode >= 400 {
		return nil, fetchErr(sourceKey, "sidecar HTTP %d", resp.StatusCode)
	}
	var fr fetchResponse
	if err := json.Unmarshal(body, &fr); err != nil {
		return nil, retryErr(sourceKey, "invalid sidecar JSON: %v", err)
	}
	if fr.Status >= 400 {
		return nil, fetchErr(sourceKey, "upstream HTTP %d", fr.Status)
	}
	if fr.Encoding == "base64" {
		decoded, err := base64.StdEncoding.DecodeString(fr.Body)
		if err != nil {
			return nil, retryErr(sourceKey, "sidecar base64 decode: %v", err)
		}
		return &Page{Body: string(decoded), ContentType: fr.ContentType}, nil
	}
	return &Page{Body: fr.Body, ContentType: fr.ContentType}, nil
}

// challengeMarkers are Cloudflare/Anubis bot-wall fingerprints (parity with
// _raise_if_challenge_page).
var challengeMarkers = []string{
	"cf-browser-verification", "challenge-running", "cdn-cgi/challenge-platform",
	"challenges.cloudflare.com", "attention required! | cloudflare",
	"anubis_version", "anubis_challenge", "making sure you're not a bot",
	"checking your browser", "enable javascript and cookies",
	"verify you are human",
}

// IsChallengePage reports whether the page is a bot challenge.
func IsChallengePage(html string) bool {
	low := strings.ToLower(html)
	for _, m := range challengeMarkers {
		if strings.Contains(low, m) {
			return true
		}
	}
	return false
}

// fmt is used in error formatting helpers.
var _ = fmt.Sprintf
