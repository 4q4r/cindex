package service

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/4q4r/cindex/backend/internal/connector"
	"github.com/4q4r/cindex/backend/internal/domain"
)

func TestPerelmanContentFetcherCollectsScreenshotAndFigure(t *testing.T) {
	png := testPNG(t, 2, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/fetch":
			var request map[string]any
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Fatal(err)
			}
			if request["url"] == "https://example.test/article" {
				_ = json.NewEncoder(w).Encode(map[string]any{
					"status":       200,
					"body":         `<html><body><img src="https://example.test/figure.png"></body></html>`,
					"content_type": "text/html",
					"encoding":     "text",
				})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"status": 200, "body": base64.StdEncoding.EncodeToString(png),
				"content_type": "image/png", "encoding": "base64",
			})
		case "/screenshot":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"status": 200, "body": base64.StdEncoding.EncodeToString(png),
				"content_type": "image/png", "encoding": "base64",
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	fetcher := newPerelmanContentFetcher(connector.NewBrowserTransport(server.URL), PerelmanConfig{MaxImages: 2})
	content, err := fetcher.fetch(t.Context(), domain.Article{URL: "https://example.test/article", FullText: "body"})
	if err != nil {
		t.Fatal(err)
	}
	if len(content.Images) != 2 || content.Images[0].Kind != "screenshot" || content.Images[1].Kind != "figure" {
		t.Fatalf("images = %#v", content.Images)
	}
}
