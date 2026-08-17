// Package connector ports the Django ingestion connectors (apps.ingestion.connectors)
// to Go: 24 source profiles, retrying HTTP transports, an HTML-mode transport via
// the browser sidecar, and per-source fetch/parse implementations.
package connector

import (
	"fmt"
	"net/url"
)

// RawArticle is the connector-agnostic article record (parity with
// apps.ingestion.connectors.base.RawArticle).
type RawArticle struct {
	SourceKey          string
	Title              string
	URL                string
	Abstract           string
	FullText           string
	Language           string
	Year               *int
	DOI                string
	Journal            string
	Authors            []string
	Volume             string
	Issue              string
	Pages              string
	PeerReviewEvidence string
	IndexingEvidence   string
	PreprintEvidence   string
	IsRetracted        bool
	RetractionNote     string
	CitedByCount       int
}

// SourceProfile carries the per-source fetch configuration (parity with
// SourceProfile in apps.ingestion.connectors.base).
type SourceProfile struct {
	SourceKey        string
	Name             string
	SearchURL        string
	Mode             string // "api", "html", "oai", "rss", "exa", ...
	QueryParam       string
	Language         string
	PeerReviewEv     string
	IndexingEv       string
	PreprintEv       string
	ResultSelector   string
	LinkSelector     string
	TitleSelector    string
	AbstractSelector string
	JournalSelector  string
}

// Profiles returns the 24 registered source profiles in the canonical order
// (parity with CONNECTORS in apps.ingestion.connectors.registry).
func Profiles() []SourceProfile {
	return []SourceProfile{
		// API-mode connectors.
		{SourceKey: "europe_pmc", Name: "Europe PMC", SearchURL: "https://www.ebi.ac.uk/europepmc/webservices/rest/search", Mode: "api", Language: "en"},
		{SourceKey: "openalex", Name: "OpenAlex", SearchURL: "https://api.openalex.org/works", Mode: "api", Language: "en"},
		{SourceKey: "crossref", Name: "Crossref", SearchURL: "https://api.crossref.org/works", Mode: "api", Language: "en"},
		{SourceKey: "pubmed", Name: "PubMed", SearchURL: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils", Mode: "api", Language: "en"},
		{SourceKey: "arxiv", Name: "arXiv", SearchURL: "http://export.arxiv.org/api/query", Mode: "api", Language: "en"},
		{SourceKey: "doaj", Name: "DOAJ", SearchURL: "https://doaj.org/api/search/articles", Mode: "api", Language: "en"},
		{SourceKey: "pmc", Name: "PubMed Central", SearchURL: "https://www.ebi.ac.uk/europepmc/webservices/rest/search", Mode: "api", Language: "en"},
		{SourceKey: "core", Name: "CORE", SearchURL: "https://api.core.ac.uk/v3/search/works", Mode: "api", Language: "en"},
		{SourceKey: "dblp", Name: "DBLP", SearchURL: "https://dblp.org/search/publ/api", Mode: "api", Language: "en"},
		{SourceKey: "hal", Name: "HAL", SearchURL: "https://api.archives-ouvertes.fr/search/", Mode: "api", Language: "fr"},
		{SourceKey: "zenodo", Name: "Zenodo", SearchURL: "https://zenodo.org/api/records", Mode: "api", Language: "en"},
		{SourceKey: "iacr", Name: "IACR ePrint", SearchURL: "https://eprint.iacr.org/rss/rss.xml", Mode: "rss", Language: "en"},
		{SourceKey: "exa", Name: "Exa", SearchURL: "https://api.exa.ai/search", Mode: "exa", Language: "multi"},
		// HTML-mode connectors (browser sidecar transport). Profile values are
		// parity with the SourceProfile blocks in html_connectors.py.
		{SourceKey: "cinii", Name: "CiNii", SearchURL: "https://cir.nii.ac.jp/opensearch/v2/articles", Mode: "api", QueryParam: "q", Language: "",
			ResultSelector: ".search-result__item, .item, article, li", LinkSelector: "a[href]", TitleSelector: "h3 a, .title a, a[href]",
			AbstractSelector: ".snippet, .description, p", JournalSelector: ".publisher, .journal, .source"},
		{SourceKey: "sciengine", Name: "SciEngine", SearchURL: "https://www.sciengine.com/SciSearch/searchNew", Mode: "api", QueryParam: "queryField_a", Language: "zh-CN"},
		{SourceKey: "cyberleninka", Name: "CyberLeninka", SearchURL: "https://cyberleninka.ru/search", Mode: "api", QueryParam: "q", Language: "ru",
			ResultSelector: ".article-item, article, li", LinkSelector: "a[href]", TitleSelector: ".title a, h2 a, h3 a, a[href]",
			AbstractSelector: ".annotation, .abstract, p", JournalSelector: ".journal, .source, .publication"},
		{SourceKey: "mathnet", Name: "MathNet.Ru", SearchURL: "https://www.mathnet.ru/php/search.phtml", Mode: "api", QueryParam: "query", Language: "ru",
			ResultSelector: "article, .source-row, .paper, .result, li", LinkSelector: "a[href]", TitleSelector: ".title a, h3 a, a[href]",
			AbstractSelector: ".abstract, .summary, p", JournalSelector: ".journal, .source"},
		{SourceKey: "scielo", Name: "SciELO", SearchURL: "https://www.scielo.org/en/search/", Mode: "rss", QueryParam: "q", Language: "es",
			ResultSelector: ".item, article, li", LinkSelector: "a[href]", TitleSelector: "h3 a, h2 a, .title a, a[href]",
			AbstractSelector: ".abstract, .snippet, p", JournalSelector: ".publication, .journal, .source"},
		{SourceKey: "persee", Name: "Persée", SearchURL: "https://www.persee.fr/search", Mode: "html", QueryParam: "q", Language: "fr",
			ResultSelector: ".doc-result", LinkSelector: "a.title", TitleSelector: "a.title",
			AbstractSelector: ".searchContext", JournalSelector: ".documentBibRef .collection a"},
		{SourceKey: "openedition", Name: "OpenEdition", SearchURL: "https://search-api.openedition.org/rss", Mode: "rss", QueryParam: "q", Language: ""},
		{SourceKey: "medknow", Name: "Medknow", SearchURL: "https://api.openalex.org/works", Mode: "api", Language: "en"},
		{SourceKey: "dergipark", Name: "DergiPark", SearchURL: "https://dergipark.org.tr/en/search", Mode: "oai", QueryParam: "q", Language: "en",
			ResultSelector: ".article-card, article, li", LinkSelector: "a[href]", TitleSelector: "h3 a, .title a, a[href]",
			AbstractSelector: ".article-abstract, .abstract, p", JournalSelector: ".journal-title, .journal, .source"},
		{SourceKey: "hrcak", Name: "Hrčak", SearchURL: "https://hrcak.srce.hr/oai/", Mode: "oai", QueryParam: "q", Language: "en",
			ResultSelector: ".search-result, article, li", LinkSelector: "a[href]", TitleSelector: "h3 a, .title a, a[href]",
			AbstractSelector: ".summary, .abstract, p", JournalSelector: ".journal, .source"},
		{SourceKey: "ajol", Name: "AJOL", SearchURL: "https://www.ajol.info/index.php/ajol/search", Mode: "oai", QueryParam: "query", Language: "en",
			ResultSelector: ".obj_article_summary, article, li", LinkSelector: "a[href]", TitleSelector: ".title a, h3 a, a[href]",
			AbstractSelector: ".summary, .abstract, p", JournalSelector: ".journal, .source"},
	}
}

// ProfileFor returns the profile for a source key.
func ProfileFor(key string) (SourceProfile, error) {
	for _, p := range Profiles() {
		if p.SourceKey == key {
			return p, nil
		}
	}
	return SourceProfile{}, fmt.Errorf("connector %q: unknown source key", key)
}

// Keys returns the canonical source key list.
func Keys() []string {
	ps := Profiles()
	keys := make([]string, 0, len(ps))
	for _, p := range ps {
		keys = append(keys, p.SourceKey)
	}
	return keys
}

// RawError distinguishes transient (retriable) failures from terminal ones,
// mirroring the Django connector error taxonomy.
type RawError struct {
	SourceKey string
	Message   string
}

func (e *RawError) Error() string { return e.SourceKey + ": " + e.Message }

// FetchError is a terminal connector failure (no retry).
type FetchError struct{ RawError }

// RetryableError is a transient failure (retried with linear backoff).
type RetryableError struct{ RawError }

// ChallengeError marks bot-wall/challenge responses so the ingest service can
// report the source as blocked rather than merely failed.
type ChallengeError struct{ RawError }

func fetchErr(key, format string, args ...any) *FetchError {
	return &FetchError{RawError{SourceKey: key, Message: fmt.Sprintf(format, args...)}}
}

func retryErr(key, format string, args ...any) *RetryableError {
	return &RetryableError{RawError{SourceKey: key, Message: fmt.Sprintf(format, args...)}}
}

// quotePlus mirrors urllib.parse.quote_plus.
func quotePlus(s string) string {
	return url.QueryEscape(s)
}
