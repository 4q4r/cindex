package domain

import (
	"strconv"
	"strings"
	"time"
)

// Sort modes (parity with apps.search.filters).
const (
	SortRelevance = "relevance"
	SortNewest    = "newest"
	SortMetadata  = "metadata"
)

// ValidSorts lists the accepted sort modes.
var ValidSorts = map[string]bool{
	SortRelevance: true,
	SortNewest:    true,
	SortMetadata:  true,
}

// SearchFilters mirrors apps.search.filters.SearchFilters.
type SearchFilters struct {
	PeerReviewedOnly bool
	IndexedOnly      bool
	ExcludePreprints bool
	ExcludeRetracted bool
	YearFrom         *int
	YearTo           *int
	SortBy           string
}

// NormalizedSort returns sort_by clamped to a valid choice.
func (f SearchFilters) NormalizedSort() string {
	if ValidSorts[f.SortBy] {
		return f.SortBy
	}
	return SortRelevance
}

// IsDefault reports whether no filter/sort overrides are active.
func (f SearchFilters) IsDefault() bool {
	return !f.PeerReviewedOnly && !f.IndexedOnly && !f.ExcludePreprints &&
		!f.ExcludeRetracted && f.YearFrom == nil && f.YearTo == nil &&
		f.NormalizedSort() == SortRelevance
}

// Signature returns a stable string identifying the filter configuration.
// Two jobs with identical query/expression but different filter signatures
// never attach to each other. Parity with apps.search.filters.signature.
func (f SearchFilters) Signature() string {
	yr := func(v *int) string {
		if v == nil {
			return ""
		}
		return strconv.Itoa(*v)
	}
	return strings.Join([]string{
		boolInt(f.PeerReviewedOnly),
		boolInt(f.IndexedOnly),
		boolInt(f.ExcludePreprints),
		boolInt(f.ExcludeRetracted),
		yr(f.YearFrom),
		yr(f.YearTo),
		f.NormalizedSort(),
	}, "|")
}

// SearchHit is one ranked article payload produced by the search service.
// Parity with apps.search.services._payload + SearchResultSerializer.
type SearchHit struct {
	ID              int64
	Title           string
	Preview         string
	Year            *int
	PublicationDate *time.Time
	Source          string
	Journal         string
	Authors         []string
	Volume          string
	Issue           string
	Pages           string
	DOI             string
	Identifiers     map[string]string
	IsPeerReviewed  bool
	Indexed         bool
	DOIAndCard      bool
	NotPreprint     bool
	PeerReviewConf  float64
	IndexingConf    float64
	DOIAndCardConf  float64
	NotPreprintConf float64
	OverallConf     float64
	IsRetracted     bool
	RetractionNote  string
	CitedByCount    int
	Tier            string
	URL             string
	RerankScore     float64
	Quotes          []Quote
	TLDR            string
}

// SearchJob statuses (parity with apps.search.models + views).
const (
	JobStatusQueued    = "queued"
	JobStatusRunning   = "running"
	JobStatusCompleted = "completed"
	JobStatusPartial   = "partial"
	JobStatusFailed    = "failed"
)

// StageProgress maps job stages to progress anchors (parity with apps.search.tasks).
var StageProgress = map[string]int{
	"queued":          5,
	"checking_index":  20,
	"live_scan":       55,
	"searching_index": 85,
	"completed":       100,
	"partial":         100,
	"failed":          100,
}

// LiveScanPhaseRatio maps live-scan substages to progress ratios.
var LiveScanPhaseRatio = map[string]float64{
	"fetching":  0.10,
	"enriching": 0.45,
	"indexing":  0.75,
	"completed": 1.0,
	"failed":    1.0,
	"skipped":   1.0,
}

// StageSubstage maps a job stage to its substage key and Russian label.
var StageSubstage = map[string][2]string{
	"queued":          {"queued", "Запрос принят"},
	"checking_index":  {"index_checking", "Проверяем корпус"},
	"live_scan":       {"source_collection", "Собираем статьи"},
	"searching_index": {"relevance_refresh", "Ранжируем статьи"},
	"completed":       {"done", "Выдача готова"},
	"failed":          {"failed", "Поиск остановлен"},
}

// SearchJob mirrors apps.search.models.SearchJob.
type SearchJob struct {
	ID                    string // uuid
	Query                 string
	Expression            string
	ForceRefreshRequested bool
	FreshnessDaysUsed     int
	Status                string
	Stage                 string
	Substage              string
	SubstageLabel         string
	Message               string
	SourceTotal           int
	SourceDone            int
	SourceLive            int
	SourceFailed          []string
	SourceTimings         map[string]SourceTiming
	IndexHitsBefore       int
	IndexHitsAfter        int
	RescanTriggered       bool
	RescanReason          string
	Results               []SearchHit
	Error                 string
	Filters               SearchFilters
	CreatedAt             time.Time
	UpdatedAt             time.Time
	FinishedAt            *time.Time
}

// SourceTiming records per-source run profile data.
type SourceTiming struct {
	Status        string  `json:"status"`
	FetchSeconds  float64 `json:"fetch_seconds"`
	EnrichSeconds float64 `json:"enrich_seconds"`
	SaveSeconds   float64 `json:"save_seconds"`
	TotalSeconds  float64 `json:"total_seconds"`
	ArticlesCount int     `json:"articles_count"`
}

// SearchWaitStat mirrors apps.search.models.SearchWaitStat.
type SearchWaitStat struct {
	Kind           string
	AverageSeconds float64
	SampleCount    int
}

// SearchWaitStat kinds.
const (
	WaitStatWithoutEnrichment = "without_enrichment"
	WaitStatWithEnrichment    = "with_enrichment"
)

func boolInt(b bool) string {
	if b {
		return "1"
	}
	return "0"
}
