package connector

import (
	"net/url"
	"strings"

	"golang.org/x/net/html"
)

// htmlNode helpers for the HTML-mode connectors.

func parseHTMLBody(body string) (*html.Node, error) {
	return html.Parse(strings.NewReader(body))
}

// walk visits every node depth-first.
func walkHTML(n *html.Node, fn func(*html.Node) bool) bool {
	if n == nil {
		return false
	}
	if fn(n) {
		return true
	}
	for c := n.FirstChild; c != nil; c = c.NextSibling {
		if walkHTML(c, fn) {
			return true
		}
	}
	return false
}

func nodeAttr(n *html.Node, key string) string {
	for _, a := range n.Attr {
		if a.Key == key {
			return a.Val
		}
	}
	return ""
}

func hasClass(n *html.Node, name string) bool {
	for _, tok := range strings.Fields(nodeAttr(n, "class")) {
		if tok == name {
			return true
		}
	}
	return false
}

// nodeText collects all descendant text, collapsing whitespace.
func nodeText(n *html.Node) string {
	var b strings.Builder
	walkHTML(n, func(c *html.Node) bool {
		if c.Type == html.TextNode {
			b.WriteString(c.Data)
			b.WriteByte(' ')
		}
		return false
	})
	return NormalizeScholarly(b.String(), -1)
}

// resolveURL joins a possibly-relative URL against the base.
func resolveURL(base, ref string) string {
	if ref == "" {
		return ""
	}
	baseURL, err := url.Parse(base)
	if err != nil {
		return ref
	}
	refURL, err := url.Parse(ref)
	if err != nil {
		return ref
	}
	return baseURL.ResolveReference(refURL).String()
}
