package connector

import (
	"context"
)

// TranslateFn translates a short query into the target language (wired to the
// service-layer translator by the caller; nil disables translation).
type TranslateFn func(ctx context.Context, query, targetLang string) string

// Options configures the connector factory (parity with the environment
// variables read by the Django connectors).
type Options struct {
	// BrowserURL is the browser sidecar base URL (CINDEX_BROWSER_URL; default
	// http://browser:8081).
	BrowserURL string
	// CoreAPIKey enables the CORE bearer token (CORE_API_KEY).
	CoreAPIKey string
	// ExaAPIKey enables Exa search (EXA_API_KEY; required for the exa source).
	ExaAPIKey string
	// Translate is the query translator used by the multi-language Exa path.
	Translate TranslateFn
	// UnpaywallEmail is the contact address required by the Unpaywall API.
	UnpaywallEmail string
	// EnableLawfulFullText enables the Unpaywall/Europe PMC OA resolver.
	EnableLawfulFullText bool
}

// MedknowHostOrganization is the OpenAlex host-organization filter that scopes
// Medknow records to its own journals (parity with the Django Medknow
// connector).
const MedknowHostOrganization = "P4310324488"

// htmlConnector adapts the HTMLEngine (dispatch by source key) to the
// Connector interface.
type htmlConnector struct {
	engine *HTMLEngine
	key    string
}

func (h htmlConnector) Key() string { return h.key }

func (h htmlConnector) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	return h.engine.Fetch(ctx, h.key, query, limit)
}

func (h htmlConnector) EnrichRaw(ctx context.Context, raw RawArticle) (*RawArticle, error) {
	return h.engine.Enrich(ctx, &raw)
}

// NewConnector builds the connector for a source key (parity with
// CONNECTORS[source_key]() in apps.ingestion.connectors.registry). Returns
// (nil, false) for unknown keys.
func NewConnector(opts Options, sourceKey string) (Connector, bool) {
	direct := NewTransport()
	engine := NewHTMLEngine(opts.BrowserURL)
	if opts.EnableLawfulFullText {
		engine.Resolver = NewLawfulFullTextResolver(engine.Browser, engine.Direct, opts.UnpaywallEmail)
	}

	switch sourceKey {
	// API-mode connectors (direct HTTP transport).
	case "europe_pmc":
		return &europePMC{apiBase: apiBase{key: sourceKey, transport: direct}}, true
	case "openalex":
		return &openalex{apiBase: apiBase{key: sourceKey, transport: direct}}, true
	case "crossref":
		return &crossrefC{apiBase{key: sourceKey, transport: direct}}, true
	case "pubmed":
		return &pubmed{apiBase{key: sourceKey, transport: direct}}, true
	case "arxiv":
		return &arxivC{apiBase{key: sourceKey, transport: direct}}, true
	case "doaj":
		return &doajC{apiBase{key: sourceKey, transport: direct}}, true
	case "pmc":
		return &europePMC{apiBase: apiBase{key: sourceKey, transport: direct}, openAccessOnly: true}, true
	case "core":
		return &coreC{apiBase: apiBase{key: sourceKey, transport: direct}, apiKey: opts.CoreAPIKey}, true
	case "dblp":
		return &dblpC{apiBase{key: sourceKey, transport: direct}}, true
	case "hal":
		return &halC{apiBase{key: sourceKey, transport: direct}}, true
	case "zenodo":
		return &zenodoC{apiBase{key: sourceKey, transport: direct}}, true
	case "iacr":
		return &iacrC{apiBase{key: sourceKey, transport: direct, browser: NewBrowserTransport(opts.BrowserURL)}}, true
	case "exa":
		return &exaC{
			apiBase:   apiBase{key: sourceKey, transport: direct},
			apiKey:    opts.ExaAPIKey,
			translate: opts.Translate,
		}, true
	// Medknow fetches through OpenAlex scoped to its own journals.
	case "medknow":
		return &openalex{apiBase: apiBase{key: sourceKey, transport: direct}, hostFilter: MedknowHostOrganization}, true
	// HTML-mode connectors (browser sidecar transport).
	case "cinii", "sciengine", "cyberleninka", "mathnet", "scielo",
		"persee", "openedition", "dergipark", "hrcak", "ajol":
		return htmlConnector{engine: engine, key: sourceKey}, true
	}
	return nil, false
}

// Registry builds fresh connector instances per source key. A new instance
// per ingest pass matches the Django registry, which instantiates
// “CONNECTORS[source_key]()“ for every run.
type Registry struct {
	opts Options
}

// NewRegistry builds the connector factory.
func NewRegistry(opts Options) *Registry {
	return &Registry{opts: opts}
}

// Get returns a fresh connector for sourceKey, or (nil, false) if unknown.
func (r *Registry) Get(sourceKey string) (Connector, bool) {
	return NewConnector(r.opts, sourceKey)
}

// All returns fresh connectors for every registered key in canonical order.
func (r *Registry) All() []Connector {
	keys := Keys()
	out := make([]Connector, 0, len(keys))
	for _, k := range keys {
		if c, ok := r.Get(k); ok {
			out = append(out, c)
		}
	}
	return out
}

// Keys returns the 24 source keys in the Django CONNECTORS order.
func (r *Registry) Keys() []string { return Keys() }

// DefaultBrowserURL mirrors DEFAULT_BROWSER_URL in the Django sidecar client.
const DefaultBrowserURL = "http://browser:8081"
