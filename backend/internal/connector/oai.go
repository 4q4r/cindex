package connector

import (
	"encoding/xml"
	"errors"
	"fmt"
	"strings"
)

// Generic recursive XML node used for OAI-PMH, RSS and Atom parsing.
type xmlNode struct {
	XMLName  xml.Name
	Attrs    []xml.Attr `xml:",any,attr"`
	Text     string     `xml:",chardata"`
	Children []xmlNode  `xml:",any"`
}

func (n *xmlNode) local() string { return n.XMLName.Local }

func (n *xmlNode) attr(name string) string {
	for _, a := range n.Attrs {
		if a.Name.Local == name {
			return a.Value
		}
	}
	return ""
}

func (n *xmlNode) child(local string) *xmlNode {
	for i := range n.Children {
		if n.Children[i].local() == local {
			return &n.Children[i]
		}
	}
	return nil
}

func (n *xmlNode) children(local string) []*xmlNode {
	var out []*xmlNode
	for i := range n.Children {
		if n.Children[i].local() == local {
			out = append(out, &n.Children[i])
		}
	}
	return out
}

// text collects chardata from the node and all descendants, preserving
// paragraph breaks for abstract-like content.
func (n *xmlNode) text() string {
	var b strings.Builder
	n.writeText(&b)
	return strings.TrimSpace(b.String())
}

func (n *xmlNode) writeText(b *strings.Builder) {
	if n.Text != "" {
		b.WriteString(n.Text)
	}
	for _, c := range n.Children {
		if len(c.Children) == 0 && c.Text != "" {
			b.WriteString(c.Text)
			b.WriteByte('\n')
			continue
		}
		c.writeText(b)
	}
}

// parseXMLRoot decodes body into a generic node tree.
func parseXMLRoot(body string) (*xmlNode, error) {
	var root xmlNode
	if err := xml.Unmarshal([]byte(body), &root); err != nil {
		return nil, fmt.Errorf("invalid XML: %w", err)
	}
	return &root, nil
}

// ---------------------------------------------------------------------------
// OAI-PMH (ListRecords).
// ---------------------------------------------------------------------------

// oaiRecord is one metadata record from a ListRecords response.
type oaiRecord struct {
	Identifier string
	Datestamp  string
	SetSpecs   []string
	Deleted    bool                // header status="deleted" (SciELO/AJOL drop these)
	Fields     map[string][]string // dc field local name -> values
	FieldOrder []string
}

// parseOAI parses an OAI-PMH ListRecords body and returns the records plus a
// resumption token (empty when done).
func parseOAI(body string) ([]oaiRecord, string, error) {
	root, err := parseXMLRoot(body)
	if err != nil {
		return nil, "", err
	}
	if root.local() != "OAI-PMH" {
		return nil, "", fmt.Errorf("not an OAI-PMH response (root %q)", root.local())
	}
	list := root.child("ListRecords")
	if list == nil {
		return nil, "", nil
	}
	var out []oaiRecord
	for _, recNode := range list.children("record") {
		rec := oaiRecord{Fields: map[string][]string{}}
		for _, part := range recNode.Children {
			switch part.local() {
			case "header":
				if part.attr("status") == "deleted" {
					rec.Deleted = true
				}
				if ds := part.child("datestamp"); ds != nil {
					rec.Datestamp = ds.text()
				}
				for _, ss := range part.children("setSpec") {
					if t := ss.text(); t != "" {
						rec.SetSpecs = append(rec.SetSpecs, t)
					}
				}
			case "metadata":
				if len(part.Children) == 0 {
					continue
				}
				dc := &part.Children[0]
				for _, f := range dc.Children {
					if t := strings.TrimSpace(f.text()); t != "" {
						rec.Fields[f.local()] = append(rec.Fields[f.local()], t)
						rec.FieldOrder = append(rec.FieldOrder, f.local())
					}
				}
			}
		}
		out = append(out, rec)
	}
	token := ""
	if rt := list.child("resumptionToken"); rt != nil {
		token = rt.text()
	}
	return out, token, nil
}

func (r *oaiRecord) first(field string) string {
	if v := r.Fields[field]; len(v) > 0 {
		return v[0]
	}
	return ""
}

// ---------------------------------------------------------------------------
// RSS 2.0 and Atom.
// ---------------------------------------------------------------------------

// feedItem is a normalized feed entry (RSS item or Atom entry).
type feedItem struct {
	Title       string
	Link        string
	Description string
	PubDate     string
	GUID        string
	Creators    []string
}

// parseFeed detects RSS vs Atom from the root element and normalizes items.
func parseFeed(body string) ([]feedItem, error) {
	root, err := parseXMLRoot(body)
	if err != nil {
		return nil, err
	}
	switch root.local() {
	case "feed":
		return parseAtom(root)
	case "rss", "RDF":
		return parseRSS(root)
	default:
		return nil, fmt.Errorf("not a feed (root %q)", root.local())
	}
}

func parseRSS(root *xmlNode) ([]feedItem, error) {
	channel := root.child("channel")
	if channel == nil {
		return nil, errors.New("rss without channel")
	}
	var out []feedItem
	for _, item := range channel.children("item") {
		it := feedItem{}
		for _, f := range item.Children {
			switch f.local() {
			case "title":
				it.Title = f.text()
			case "link":
				it.Link = f.text()
			case "description":
				it.Description = f.text()
			case "pubDate", "date":
				it.PubDate = f.text()
			case "guid":
				it.GUID = f.text()
			case "creator", "author":
				if t := f.text(); t != "" {
					it.Creators = append(it.Creators, t)
				}
			}
		}
		if it.Link == "" {
			it.Link = it.GUID
		}
		out = append(out, it)
	}
	return out, nil
}

func parseAtom(root *xmlNode) ([]feedItem, error) {
	var out []feedItem
	for _, entry := range root.children("entry") {
		it := feedItem{}
		for _, f := range entry.Children {
			switch f.local() {
			case "title":
				it.Title = f.text()
			case "summary", "content":
				if it.Description == "" {
					it.Description = f.text()
				}
			case "published", "updated", "modified":
				if it.PubDate == "" {
					it.PubDate = f.text()
				}
			case "id":
				it.GUID = f.text()
			case "link":
				rel := f.attr("rel")
				if rel == "" || rel == "alternate" {
					it.Link = f.attr("href")
				}
			case "author", "creator":
				if name := f.child("name"); name != nil {
					it.Creators = append(it.Creators, name.text())
				} else if t := f.text(); t != "" {
					it.Creators = append(it.Creators, t)
				}
			}
		}
		if it.Link == "" {
			it.Link = it.GUID
		}
		out = append(out, it)
	}
	return out, nil
}
