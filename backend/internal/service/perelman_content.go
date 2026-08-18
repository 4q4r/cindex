package service

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"image"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"net/url"
	"path"
	"strings"

	"github.com/4q4r/cindex/backend/internal/connector"
	"github.com/4q4r/cindex/backend/internal/domain"
	"golang.org/x/net/html"
)

type perelmanContent struct {
	Text   string
	Images []perelmanImage
}

type perelmanContentFetcher struct {
	browser   *connector.BrowserTransport
	maxPages  int
	dpi       int
	maxImages int
}

func newPerelmanContentFetcher(browser *connector.BrowserTransport, cfg PerelmanConfig) *perelmanContentFetcher {
	maxPages := cfg.MaxPDFPages
	if maxPages <= 0 {
		maxPages = 8
	}
	dpi := cfg.PDFDPI
	if dpi <= 0 {
		dpi = 144
	}
	maxImages := cfg.MaxImages
	if maxImages <= 0 {
		maxImages = 8
	}
	return &perelmanContentFetcher{browser: browser, maxPages: maxPages, dpi: dpi, maxImages: maxImages}
}

func (f *perelmanContentFetcher) fetch(ctx context.Context, article domain.Article) (perelmanContent, error) {
	base := articleInput(article)
	if f == nil || f.browser == nil || strings.TrimSpace(article.URL) == "" {
		return perelmanContent{Text: base}, nil
	}
	page, err := f.browser.Fetch(ctx, "perelman", article.URL, nil, "", 60)
	if err != nil {
		return perelmanContent{Text: base}, nil
	}
	if isPDF(page.ContentType, article.URL) {
		return f.pdf(ctx, base, []byte(page.Body))
	}
	document, err := html.Parse(strings.NewReader(page.Body))
	if err != nil {
		return perelmanContent{Text: base}, nil
	}
	if pdfURL := findPDFURL(document, article.URL); pdfURL != "" {
		if pdfPage, pdfErr := f.browser.Fetch(ctx, "perelman", pdfURL, nil, "application/pdf", 60); pdfErr == nil && isPDF(pdfPage.ContentType, pdfURL) {
			content, pdfErr := f.pdf(ctx, base, []byte(pdfPage.Body))
			if pdfErr == nil && len(content.Images) > 0 {
				return content, nil
			}
		}
	}
	return f.html(ctx, base, article.URL, document)
}

func (f *perelmanContentFetcher) pdf(ctx context.Context, base string, data []byte) (perelmanContent, error) {
	pages, err := f.browser.PDFPages(ctx, "perelman", data, "eng", f.maxPages, f.dpi)
	if err != nil {
		return perelmanContent{Text: base}, err
	}
	content := perelmanContent{Text: joinText(base, pages.Text)}
	for _, page := range pages.Pages {
		imageData, err := decodeTransportImage(page.Body, page.Encoding)
		if err != nil {
			continue
		}
		content.Images = append(content.Images, perelmanImage{
			ID: page.ID, Data: imageData, MIME: page.ContentType, Kind: "pdf-page",
			Width: page.Width, Height: page.Height,
		})
	}
	return content, nil
}

func (f *perelmanContentFetcher) html(ctx context.Context, base, articleURL string, document *html.Node) (perelmanContent, error) {
	content := perelmanContent{Text: base}
	if screenshot, err := f.browser.Screenshot(ctx, "perelman", articleURL, 60); err == nil {
		if width, height, err := imageDimensions(screenshot); err == nil {
			content.Images = append(content.Images, perelmanImage{ID: "page-shot", Data: screenshot, MIME: "image/png", Kind: "screenshot", Width: width, Height: height})
		}
	}
	for _, imageURL := range figureURLs(document, articleURL) {
		if len(content.Images) >= f.maxImages+1 {
			break
		}
		page, err := f.browser.Fetch(ctx, "perelman", imageURL, nil, "image/png,image/jpeg,image/gif", 30)
		if err != nil {
			continue
		}
		data, err := decodeTransportImage(page.Body, page.Encoding)
		if err != nil {
			continue
		}
		width, height, err := imageDimensions(data)
		if err != nil || !supportedImageMIME(page.ContentType) {
			continue
		}
		content.Images = append(content.Images, perelmanImage{ID: fmt.Sprintf("fig-%d", len(content.Images)), Data: data, MIME: imageMIME(page.ContentType), Kind: "figure", Width: width, Height: height})
	}
	return content, nil
}

func decodeTransportImage(body, encoding string) ([]byte, error) {
	if encoding == "base64" {
		return base64.StdEncoding.DecodeString(body)
	}
	return []byte(body), nil
}

func imageDimensions(data []byte) (int, int, error) {
	config, _, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return 0, 0, err
	}
	return config.Width, config.Height, nil
}

func imageMIME(contentType string) string {
	contentType = strings.ToLower(strings.Split(contentType, ";")[0])
	if supportedImageMIME(contentType) {
		return contentType
	}
	return "image/png"
}

func supportedImageMIME(contentType string) bool {
	contentType = imageMIMEBase(contentType)
	return contentType == "image/png" || contentType == "image/jpeg" || contentType == "image/gif"
}

func imageMIMEBase(contentType string) string {
	return strings.ToLower(strings.TrimSpace(strings.Split(contentType, ";")[0]))
}

func isPDF(contentType, rawURL string) bool {
	return strings.Contains(strings.ToLower(contentType), "application/pdf") || strings.HasSuffix(strings.ToLower(strings.Split(rawURL, "?")[0]), ".pdf")
}

func joinText(parts ...string) string {
	var nonEmpty []string
	for _, part := range parts {
		if strings.TrimSpace(part) != "" {
			nonEmpty = append(nonEmpty, strings.TrimSpace(part))
		}
	}
	return strings.Join(nonEmpty, "\n\n")
}

func findPDFURL(root *html.Node, base string) string {
	var found string
	walkHTML(root, func(node *html.Node) {
		if found != "" || node.Type != html.ElementNode {
			return
		}
		if node.Data == "meta" {
			isPDFMeta := false
			content := ""
			for _, attr := range node.Attr {
				if (attr.Key == "name" || attr.Key == "property") && strings.Contains(strings.ToLower(attr.Val), "pdf") {
					isPDFMeta = true
				}
				if attr.Key == "content" {
					content = attr.Val
				}
			}
			if isPDFMeta {
				found = resolveURL(base, content)
			}
			return
		}
		for _, attr := range node.Attr {
			if node.Data == "a" && attr.Key == "href" && strings.Contains(strings.ToLower(attr.Val), "pdf") {
				found = resolveURL(base, attr.Val)
				return
			}
		}
	})
	return found
}

func figureURLs(root *html.Node, base string) []string {
	seen := make(map[string]struct{})
	var result []string
	walkHTML(root, func(node *html.Node) {
		if node.Type != html.ElementNode || node.Data != "img" {
			return
		}
		for _, attr := range node.Attr {
			if attr.Key != "src" && attr.Key != "data-src" {
				continue
			}
			resolved := resolveURL(base, attr.Val)
			ext := strings.ToLower(path.Ext(strings.Split(resolved, "?")[0]))
			if ext != ".png" && ext != ".jpg" && ext != ".jpeg" && ext != ".gif" {
				continue
			}
			if _, ok := seen[resolved]; !ok {
				seen[resolved] = struct{}{}
				result = append(result, resolved)
			}
			break
		}
	})
	return result
}

func walkHTML(node *html.Node, visit func(*html.Node)) {
	visit(node)
	for child := node.FirstChild; child != nil; child = child.NextSibling {
		walkHTML(child, visit)
	}
}

func resolveURL(base, raw string) string {
	baseURL, err := url.Parse(base)
	if err != nil {
		return ""
	}
	child, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return ""
	}
	return baseURL.ResolveReference(child).String()
}
