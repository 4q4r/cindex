package connector

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBrowserTransportPDFText(t *testing.T) {
	var gotLanguage string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/pdf-text" {
			http.NotFound(w, r)
			return
		}
		var payload pdfTextRequest
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		gotLanguage = payload.OCRLanguage
		body, err := base64.StdEncoding.DecodeString(payload.Body)
		if err != nil || string(body) != "%PDF-test" {
			t.Fatalf("PDF payload = %q, err=%v", body, err)
		}
		_ = json.NewEncoder(w).Encode(pdfTextResponse{Text: "  extracted\n text  "})
	}))
	defer server.Close()

	browser := NewBrowserTransport(server.URL)
	text, err := browser.PDFText(context.Background(), "test", []byte("%PDF-test"), "rus")
	if err != nil {
		t.Fatal(err)
	}
	if text != "extracted text" || gotLanguage != "rus" {
		t.Fatalf("PDFText = %q, language=%q", text, gotLanguage)
	}
}

func TestLawfulFullTextResolverCascade(t *testing.T) {
	var fetched []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/unpaywall/"):
			_, _ = io.WriteString(w, `{"best_oa_location":{"url_for_pdf":"https://oa.test/one.pdf","url_for_landing_page":"https://oa.test/landing"},"oa_locations":[{"url_for_pdf":"https://oa.test/one.pdf"}]}`)
		case r.Method == http.MethodGet && r.URL.Path == "/europe":
			_, _ = io.WriteString(w, `{"resultList":{"result":[{"fullTextUrlList":{"fullTextUrl":[{"url":"https://oa.test/two.pdf"}]}}]}}`)
		case r.Method == http.MethodPost && r.URL.Path == "/fetch":
			var request fetchRequest
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Fatal(err)
			}
			fetched = append(fetched, request.URL)
			if request.URL == "https://oa.test/landing" {
				_ = json.NewEncoder(w).Encode(fetchResponse{
					Status: 200, ContentType: "text/html",
					Body: `<html><head><meta name="citation_pdf_url" content="https://oa.test/nested.pdf"></head><body>landing text<a href="/nested.pdf">PDF</a></body></html>`,
				})
				return
			}
			marker := "%PDF-two"
			if strings.Contains(request.URL, "one.pdf") {
				marker = "%PDF-one"
			} else if strings.Contains(request.URL, "nested.pdf") {
				marker = "%PDF-nested"
			}
			_ = json.NewEncoder(w).Encode(fetchResponse{
				Status: 200, Body: base64.StdEncoding.EncodeToString([]byte(marker)),
				ContentType: "application/pdf", Encoding: "base64",
			})
		case r.Method == http.MethodPost && r.URL.Path == "/pdf-text":
			var request pdfTextRequest
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Fatal(err)
			}
			body, _ := base64.StdEncoding.DecodeString(request.Body)
			text := "second lawful text"
			if strings.Contains(string(body), "one") {
				text = "first lawful text"
			} else if strings.Contains(string(body), "nested") {
				text = "landing PDF text"
			}
			_ = json.NewEncoder(w).Encode(pdfTextResponse{Text: text})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	direct := NewTransport()
	direct.Client = server.Client()
	browser := NewBrowserTransport(server.URL)
	resolver := NewLawfulFullTextResolver(browser, direct, "test@example.org")
	resolver.UnpaywallBase = server.URL + "/unpaywall"
	resolver.EuropePMCBase = server.URL + "/europe"
	resolver.URLAllowed = func(context.Context, string) bool { return true }
	got := resolver.Resolve(context.Background(), &RawArticle{
		SourceKey: "ajol", DOI: "10.5555/test", Language: "en",
	}, "existing text")
	for _, want := range []string{"existing text", "first lawful text", "landing PDF text", "second lawful text"} {
		if !strings.Contains(got, want) {
			t.Errorf("resolved text %q missing %q", got, want)
		}
	}
	wantFetches := []string{
		"https://oa.test/one.pdf", "https://oa.test/landing",
		"https://oa.test/nested.pdf", "https://oa.test/two.pdf",
	}
	if len(fetched) != len(wantFetches) {
		t.Fatalf("fetched = %v", fetched)
	}
	for i := range wantFetches {
		if fetched[i] != wantFetches[i] {
			t.Errorf("fetch[%d] = %q, want %q", i, fetched[i], wantFetches[i])
		}
	}
}

func TestLawfulFullTextResolverNoDOIPreservesExisting(t *testing.T) {
	resolver := NewLawfulFullTextResolver(nil, nil, "")
	if got := resolver.Resolve(context.Background(), &RawArticle{}, " existing\ntext "); got != "existing text" {
		t.Errorf("Resolve = %q", got)
	}
}

func TestPublicHTTPURLRejectsInternalDestinations(t *testing.T) {
	ctx := context.Background()
	for _, candidate := range []string{
		"http://127.0.0.1/admin", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
		"ftp://example.com/file", "http://user:pass@example.com/", "http://localhost/",
	} {
		if publicHTTPURL(ctx, candidate) {
			t.Errorf("publicHTTPURL accepted %q", candidate)
		}
	}
}
