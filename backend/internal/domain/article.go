// Package domain holds the core entities and pure business rules of CIndex.
package domain

import "time"

// Source is a connector source (arXiv, Crossref, OpenAlex, ...).
type Source struct {
	ID                  int64
	Key                 string
	Name                string
	BaseURL             string
	Active              bool
	TotalRuns           int
	TotalSuccesses      int
	TotalFailures       int
	ConsecutiveFailures int
	LastCheckedAt       *time.Time
	LastSuccessAt       *time.Time
	CircuitOpenUntil    *time.Time
	LastError           string
}

// Journal is a journal card used for the doi+journal trust signal.
type Journal struct {
	ID        int64
	Name      string
	ISSN      string
	EISSN     string
	Publisher string
}

// Article is a normalized scholarly article record.
type Article struct {
	ID           int64
	SourceID     int64
	JournalID    *int64
	Journal      *Journal
	ExternalID   string
	Title        string
	Abstract     string
	FullText     string
	Language     string
	PubYear      *int
	PubDate      *time.Time
	URL          string
	DOI          string
	LocalMDPath  string
	Volume       string
	Issue        string
	Pages        string
	IsOpenAccess bool

	IsRetracted    bool
	RetractionNote string
	CitedByCount   int

	IsPeerReviewedOrRefereed  bool
	IsIndexedInReputableDB    bool
	HasDOIAndJournalCard      bool
	IsNotPreprintOrManuscript bool
	SearchVector              string
	IsEligible                bool

	PeerReviewConfidence  float64
	IndexingConfidence    float64
	DOIAndCardConfidence  float64
	NotPreprintConfidence float64
	EligibilityConfidence float64

	PeerReviewEvidence string
	IndexingEvidence   string
	DOIJournalEvidence string
	PreprintEvidence   string

	CreatedAt time.Time
	UpdatedAt time.Time
}

// Author is a normalized author record.
type Author struct {
	ID       int64
	FullName string
}

// ArticleAuthor links an author to an article with display order.
type ArticleAuthor struct {
	ArticleID int64
	AuthorID  int64
	FullName  string
	Order     int
}

// Identifier is a typed extra identifier for an article (e.g. HAL ID).
type Identifier struct {
	ArticleID int64
	Kind      string
	Value     string
}

// Quote is one verbatim quote extracted by PERELMAN.
type Quote struct {
	Text      string  `json:"text"`
	Location  string  `json:"location"`
	Relevance float64 `json:"relevance"`
	Rationale string  `json:"rationale"`
}

// ArticleQuotes is the per-article PERELMAN cache/claim row.
type ArticleQuotes struct {
	ArticleID   int64
	Quotes      []Quote
	TLDR        string
	Status      string
	ExtractedAt *time.Time
	Model       string
	Error       string
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

// ArticleQuotes statuses (parity with apps.extraction.models).
const (
	QuotesStatusPending = "pending"
	QuotesStatusDone    = "done"
	QuotesStatusFailed  = "failed"
	QuotesStatusNoText  = "no_text"
)
