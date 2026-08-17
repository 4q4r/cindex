package domain

import (
	"regexp"
	"strings"
)

// Peer-review / indexing tier protocol. Connectors encode the strength of the
// per-record signal as a leading tier prefix on the *Evidence text fields:
//
//	tierA: explicit per-record confirmation from the source API (confidence 1.0)
//	tierB: venue / source-reputation inference (confidence 0.7)
//
// Plus a conservative source-reputation fallback for records ingested before
// connectors emitted tier signals (confidence 0.6) and a keyword scan
// inference (confidence 0.3). Parity with apps.articles.services.
const (
	TierAPrefix = "tierA:"
	TierBPrefix = "tierB:"

	ConfidenceTierA         = 1.0
	ConfidenceTierB         = 0.7
	ConfidenceSourceDefault = 0.6
	ConfidenceKeyword       = 0.3
	ConfidenceNone          = 0.0
)

// PeerReviewedByDefault lists sources whose entire corpus is peer-reviewed by
// reputation; used only as a Tier B fallback when no per-record evidence is
// present. Mixed repositories (Zenodo, CORE, HAL, DBLP) are deliberately
// omitted so their records stay "unverified".
var PeerReviewedByDefault = map[string]bool{
	"pubmed": true, "pmc": true, "europe_pmc": true, "doaj": true, "scielo": true, "mathnet": true,
}

// PreprintSources lists sources whose entire corpus is preprints.
var PreprintSources = map[string]bool{"arxiv": true, "iacr": true}

// IndexingKeywords and friends mirror the classifier keyword lists.
var (
	IndexingKeywords = []string{
		"scopus", "web of science", "medline", "pmc", "pubmed", "pubmed central",
		"kci", "tr dizin", "esci", "doaj", "openalex", "crossref", "semantic scholar",
	}
	PreprintKeywords = []string{
		"preprint", "author manuscript", "accepted manuscript", "working paper",
	}
	PeerReviewKeywords = []string{
		"peer reviewed", "peer-review", "refereed", "double blind review",
	}
)

var doiPattern = regexp.MustCompile(`(?i)10\.\d{4,9}/[-._;()/:A-Z0-9]+`)

func hasTier(evidence, tier string) bool {
	return evidence != "" && strings.HasPrefix(evidence, tier)
}

// TierLabel returns the peer-review trust tier label for a search-result
// card, derived from the persisted confidence. Parity with
// apps.articles.services.tier_label.
func TierLabel(isPeerReviewed bool, confidence float64) string {
	if !isPeerReviewed {
		return "none"
	}
	switch {
	case confidence >= ConfidenceTierA:
		return "A"
	case confidence >= ConfidenceTierB:
		return "B"
	case confidence >= ConfidenceSourceDefault:
		return "source-default"
	case confidence >= ConfidenceKeyword:
		return "keyword"
	default:
		return "none"
	}
}

// EligibilityDecision is the boolean and confidence outcome of eligibility
// evaluation. Parity with apps.articles.services.EligibilityDecision.
type EligibilityDecision struct {
	PeerReviewed     bool
	Indexed          bool
	DOIAndCard       bool
	NotPreprint      bool
	PeerReviewConf   float64
	IndexingConf     float64
	DOIAndCardConf   float64
	NotPreprintConf  float64
	PeerReviewReason string
	PreprintReason   string
	Retracted        bool
}

// Eligible mirrors the Django property: a retracted article is never eligible.
func (d EligibilityDecision) Eligible() bool {
	return !d.Retracted && d.PeerReviewed && d.Indexed && d.DOIAndCard && d.NotPreprint
}

// OverallConfidence mirrors the Django property.
func (d EligibilityDecision) OverallConfidence() float64 {
	return round4((d.PeerReviewConf + d.IndexingConf + d.DOIAndCardConf + d.NotPreprintConf) / 4.0)
}

func round4(v float64) float64 {
	return float64(int64(v*10000+0.5)) / 10000
}

// EvaluateEligibility evaluates peer-review / preprint / indexed eligibility
// by tier precedence. Parity with
// apps.articles.services.ArticleEligibilityService.evaluate.
//
// Precedence: preprint override > tierA > tierB > source default > keyword
// scan > unverified. A preprint is never peer-reviewed. A retracted article
// is additionally never eligible.
func EvaluateEligibility(a *Article, sourceKey, journalName string) EligibilityDecision {
	peerEv := a.PeerReviewEvidence
	indexEv := a.IndexingEvidence
	preprintEv := a.PreprintEvidence

	// Keyword scan runs over title/abstract/fulltext only -- NOT over the
	// evidence fields -- so a reason written on a prior pass (or a connector
	// tier string) cannot fabricate a keyword match.
	scanText := strings.ToLower(strings.Join([]string{
		a.Title, a.Abstract, truncate(a.FullText, 5000),
	}, " "))

	// --- preprint (overrides peer-review) ---
	isPreprint, preprintReason := decidePreprint(preprintEv, scanText, sourceKey)
	notPreprint := !isPreprint

	// --- peer-reviewed ---
	peerReviewed, peerReviewConf, peerReviewReason := decidePeerReview(peerEv, scanText, sourceKey, isPreprint)

	// --- indexed in a reputable DB ---
	indexed := false
	indexingConf := ConfidenceNone
	if hasTier(indexEv, TierAPrefix) || hasTier(indexEv, TierBPrefix) {
		indexed = true
		if hasTier(indexEv, TierAPrefix) {
			indexingConf = ConfidenceTierA
		} else {
			indexingConf = ConfidenceTierB
		}
	} else {
		for _, tok := range IndexingKeywords {
			if strings.Contains(scanText, tok) {
				indexed = true
				break
			}
		}
		if indexed {
			indexingConf = ConfidenceKeyword
		}
	}

	hasDOI := doiPattern.MatchString(a.DOI) || doiPattern.MatchString(scanText)
	journalCard := journalName != ""
	doiAndCard := hasDOI && journalCard
	doiConf := 0.0
	if hasDOI {
		doiConf = 1.0
	}
	journalConf := 0.0
	if journalCard {
		journalConf = 1.0
	}
	doiAndCardConf := round4((doiConf + journalConf) / 2.0)
	notPreprintConf := 0.0
	if notPreprint {
		notPreprintConf = 1.0
	}

	return EligibilityDecision{
		PeerReviewed:     peerReviewed,
		Indexed:          indexed,
		DOIAndCard:       doiAndCard,
		NotPreprint:      notPreprint,
		PeerReviewConf:   peerReviewConf,
		IndexingConf:     indexingConf,
		DOIAndCardConf:   doiAndCardConf,
		NotPreprintConf:  notPreprintConf,
		PeerReviewReason: peerReviewReason,
		PreprintReason:   preprintReason,
		Retracted:        a.IsRetracted,
	}
}

func decidePreprint(preprintEv, scanText, sourceKey string) (bool, string) {
	if hasTier(preprintEv, TierAPrefix) {
		return true, ""
	}
	if PreprintSources[sourceKey] {
		return true, "препринт-источник: " + sourceKey
	}
	for _, tok := range PreprintKeywords {
		if strings.Contains(scanText, tok) {
			return true, "упоминание препринта в тексте"
		}
	}
	return false, ""
}

func decidePeerReview(peerEv, scanText, sourceKey string, isPreprint bool) (bool, float64, string) {
	if isPreprint {
		return false, ConfidenceTierA, ""
	}
	if hasTier(peerEv, TierAPrefix) {
		return true, ConfidenceTierA, ""
	}
	if hasTier(peerEv, TierBPrefix) {
		return true, ConfidenceTierB, ""
	}
	if PeerReviewedByDefault[sourceKey] {
		return true, ConfidenceSourceDefault, "рецензируемый источник по репутации: " + sourceKey
	}
	for _, tok := range PeerReviewKeywords {
		if strings.Contains(scanText, tok) {
			return true, ConfidenceKeyword, "упоминание рецензирования в тексте"
		}
	}
	return false, ConfidenceNone, ""
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// EligibilityUpdate is the set of article fields derived from an eligibility
// decision, ready for persistence. Connector-set tier evidence is preserved
// verbatim; on the source-default or keyword path the classifier fills a
// human-readable Russian reason into the (empty) evidence field, and a
// retracted article without a note gets the default Russian note. Parity with
// apps.articles.services.ArticleEligibilityService.apply (minus persistence).
type EligibilityUpdate struct {
	IsPeerReviewedOrRefereed  bool
	IsIndexedInReputableDB    bool
	HasDOIAndJournalCard      bool
	IsNotPreprintOrManuscript bool
	IsEligible                bool
	PeerReviewConfidence      float64
	IndexingConfidence        float64
	DOIAndCardConfidence      float64
	NotPreprintConfidence     float64
	EligibilityConfidence     float64
	PeerReviewEvidence        string
	PreprintEvidence          string
	RetractionNote            string
}

// ApplyEligibility computes the persistence-ready eligibility update for an
// article given its source key and journal name.
func ApplyEligibility(a *Article, sourceKey, journalName string) EligibilityUpdate {
	d := EvaluateEligibility(a, sourceKey, journalName)
	u := EligibilityUpdate{
		IsPeerReviewedOrRefereed:  d.PeerReviewed,
		IsIndexedInReputableDB:    d.Indexed,
		HasDOIAndJournalCard:      d.DOIAndCard,
		IsNotPreprintOrManuscript: d.NotPreprint,
		IsEligible:                d.Eligible(),
		PeerReviewConfidence:      d.PeerReviewConf,
		IndexingConfidence:        d.IndexingConf,
		DOIAndCardConfidence:      d.DOIAndCardConf,
		NotPreprintConfidence:     d.NotPreprintConf,
		EligibilityConfidence:     d.OverallConfidence(),
		PeerReviewEvidence:        a.PeerReviewEvidence,
		PreprintEvidence:          a.PreprintEvidence,
		RetractionNote:            a.RetractionNote,
	}
	if d.PeerReviewReason != "" && a.PeerReviewEvidence == "" {
		u.PeerReviewEvidence = d.PeerReviewReason
	}
	if d.PreprintReason != "" && a.PreprintEvidence == "" {
		u.PreprintEvidence = d.PreprintReason
	}
	if d.Retracted && a.RetractionNote == "" {
		u.RetractionNote = "статья отозвана (retraction)"
	}
	return u
}
