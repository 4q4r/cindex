package domain

import (
	"testing"
)

func baseArticle() *Article {
	return &Article{
		Title:            "Testing peer review in scholarly search",
		Abstract:         "An abstract mentioning Scopus and peer reviewed content.",
		DOI:              "10.1234/example.5678",
		IsRetracted:      false,
		FullText:         "",
		PreprintEvidence: "",
	}
}

func TestTierLabels(t *testing.T) {
	cases := []struct {
		name       string
		peer       bool
		confidence float64
		want       string
	}{
		{"unverified", false, 0.0, "none"},
		{"not peer reviewed regardless of confidence", false, 1.0, "none"},
		{"tier a", true, 1.0, "A"},
		{"tier b", true, 0.7, "B"},
		{"source default", true, 0.6, "source-default"},
		{"keyword", true, 0.3, "keyword"},
		{"below keyword floor", true, 0.1, "none"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := TierLabel(tc.peer, tc.confidence); got != tc.want {
				t.Errorf("TierLabel(%v, %v) = %q, want %q", tc.peer, tc.confidence, got, tc.want)
			}
		})
	}
}

func TestEvaluateTierAPeerReviewWins(t *testing.T) {
	a := baseArticle()
	a.PeerReviewEvidence = "tierA: openalex venue=journal peer-review explicit"
	d := EvaluateEligibility(a, "openalex", "Journal of Testing")
	if !d.PeerReviewed {
		t.Fatal("want peer reviewed")
	}
	if d.PeerReviewConf != ConfidenceTierA {
		t.Errorf("confidence = %v, want 1.0", d.PeerReviewConf)
	}
	if d.PeerReviewReason != "" {
		t.Errorf("reason must stay empty on tierA, got %q", d.PeerReviewReason)
	}
}

func TestEvaluatePreprintOverrideBeatsPeerReview(t *testing.T) {
	a := baseArticle()
	a.PeerReviewEvidence = "tierA: peer-reviewed journal"
	a.PreprintEvidence = "tierA: preprint"
	d := EvaluateEligibility(a, "openalex", "Journal of Testing")
	if d.PeerReviewed {
		t.Fatal("preprint must never be peer reviewed")
	}
	if d.NotPreprint {
		t.Fatal("expected preprint")
	}
	if d.Eligible() {
		t.Fatal("preprint must not be eligible")
	}
}

func TestEvaluatePreprintSourceReputation(t *testing.T) {
	a := baseArticle()
	a.PreprintEvidence = ""
	a.DOI = ""
	a.Title = "A paper about embeddings"
	a.Abstract = ""
	d := EvaluateEligibility(a, "arxiv", "")
	if d.NotPreprint {
		t.Fatal("arxiv is a preprint source")
	}
	if d.PreprintReason != "препринт-источник: arxiv" {
		t.Errorf("reason = %q", d.PreprintReason)
	}
}

func TestEvaluatePreprintKeywordInText(t *testing.T) {
	a := baseArticle()
	a.FullText = "This is an accepted manuscript version."
	a.PreprintEvidence = ""
	d := EvaluateEligibility(a, "zenodo", "")
	if !d.NotPreprint == d.PeerReviewed {
		t.Fatal("accepted manuscript keyword must mark preprint")
	}
	if d.PreprintReason != "упоминание препринта в тексте" {
		t.Errorf("reason = %q", d.PreprintReason)
	}
}

func TestEvaluateTierBSourceDefaultKeywordPrecedence(t *testing.T) {
	a := baseArticle()
	a.PeerReviewEvidence = ""
	a.FullText = ""

	// tierB explicit wins over source default
	a.PeerReviewEvidence = "tierB: venue reputation"
	d := EvaluateEligibility(a, "pubmed", "Some Journal")
	if d.PeerReviewConf != ConfidenceTierB {
		t.Errorf("tierB confidence = %v", d.PeerReviewConf)
	}

	// source default wins over keyword scan
	a.PeerReviewEvidence = ""
	d = EvaluateEligibility(a, "pubmed", "Some Journal")
	if d.PeerReviewConf != ConfidenceSourceDefault {
		t.Errorf("source default confidence = %v", d.PeerReviewConf)
	}
	if d.PeerReviewReason == "" {
		t.Error("source default must fill a reason")
	}

	// keyword scan is weakest
	d = EvaluateEligibility(a, "zenodo", "Some Journal")
	if d.PeerReviewConf != ConfidenceKeyword {
		t.Errorf("keyword confidence = %v", d.PeerReviewConf)
	}
	if d.PeerReviewReason != "упоминание рецензирования в тексте" {
		t.Errorf("reason = %q", d.PeerReviewReason)
	}
}

func TestEvaluateKeywordScanIgnoresEvidenceFields(t *testing.T) {
	a := baseArticle()
	a.PeerReviewEvidence = "рецензируемый источник по репутации: pubmed"
	a.Abstract = ""
	a.FullText = ""
	d := EvaluateEligibility(a, "zenodo", "")
	if d.PeerReviewed {
		t.Fatal("evidence text must not fabricate a keyword match")
	}
}

func TestEvaluateIndexingTierAndKeyword(t *testing.T) {
	a := baseArticle()
	a.IndexingEvidence = "tierA: medline"
	d := EvaluateEligibility(a, "zenodo", "Some Journal")
	if !d.Indexed || d.IndexingConf != ConfidenceTierA {
		t.Errorf("tierA indexing: %+v", d)
	}

	a.IndexingEvidence = ""
	a.Abstract = "Indexed in Scopus and Medline."
	d = EvaluateEligibility(a, "zenodo", "Some Journal")
	if !d.Indexed || d.IndexingConf != ConfidenceKeyword {
		t.Errorf("keyword indexing: %+v", d)
	}

	a.Abstract = ""
	d = EvaluateEligibility(a, "zenodo", "Some Journal")
	if d.Indexed {
		t.Error("no keywords must mean not indexed")
	}
}

func TestEvaluateDOIAndJournalCard(t *testing.T) {
	a := baseArticle()
	a.DOI = "10.1234/example.5678"
	d := EvaluateEligibility(a, "zenodo", "Some Journal")
	if !d.DOIAndCard || d.DOIAndCardConf != 1.0 {
		t.Errorf("doi+journal: %+v", d)
	}

	d = EvaluateEligibility(a, "zenodo", "")
	if d.DOIAndCard || d.DOIAndCardConf != 0.5 {
		t.Errorf("doi only: %+v", d)
	}

	a.DOI = ""
	d = EvaluateEligibility(a, "zenodo", "")
	if d.DOIAndCard || d.DOIAndCardConf != 0.0 {
		t.Errorf("nothing: %+v", d)
	}
}

func TestEvaluateDOIInTextFallback(t *testing.T) {
	a := baseArticle()
	a.DOI = ""
	a.Abstract = "See 10.1000/xyz123/details for the full text"
	d := EvaluateEligibility(a, "zenodo", "Some Journal")
	if !d.DOIAndCard {
		t.Fatal("DOI embedded in text must count")
	}
}

func TestEvaluateRetractionNeverEligible(t *testing.T) {
	a := baseArticle()
	a.IsRetracted = true
	d := EvaluateEligibility(a, "openalex", "Some Journal")
	if d.Eligible() {
		t.Fatal("retracted article must never be eligible")
	}
	if !d.Retracted {
		t.Fatal("decision must carry retracted flag")
	}
	// Peer-review flags stay visible for transparency.
	if !d.PeerReviewed {
		t.Fatal("retraction must not touch peer-review flags")
	}
}

func TestOverallConfidenceRounding(t *testing.T) {
	d := EligibilityDecision{
		PeerReviewConf: 1.0, IndexingConf: 1.0, DOIAndCardConf: 1.0, NotPreprintConf: 1.0,
	}
	if got := d.OverallConfidence(); got != 1.0 {
		t.Errorf("overall = %v", got)
	}
	d = EligibilityDecision{
		PeerReviewConf: 0.7, IndexingConf: 0.3, DOIAndCardConf: 0.5, NotPreprintConf: 1.0,
	}
	if got := d.OverallConfidence(); got != 0.625 {
		t.Errorf("overall = %v, want 0.625", got)
	}
}

func TestApplyEligibilityFillsReasons(t *testing.T) {
	a := baseArticle()
	a.PeerReviewEvidence = ""
	a.FullText = "peer reviewed journal article"
	u := ApplyEligibility(a, "zenodo", "Some Journal")
	if u.PeerReviewEvidence != "упоминание рецензирования в тексте" {
		t.Errorf("evidence = %q", u.PeerReviewEvidence)
	}
	if !u.IsEligible {
		t.Error("expected eligible")
	}
}

func TestApplyEligibilityPreservesTierEvidence(t *testing.T) {
	a := baseArticle()
	a.PeerReviewEvidence = "tierA: openalex"
	u := ApplyEligibility(a, "zenodo", "Some Journal")
	if u.PeerReviewEvidence != "tierA: openalex" {
		t.Errorf("tier evidence must be preserved verbatim, got %q", u.PeerReviewEvidence)
	}
}

func TestApplyEligibilityRetractionNote(t *testing.T) {
	a := baseArticle()
	a.IsRetracted = true
	a.RetractionNote = ""
	u := ApplyEligibility(a, "openalex", "Some Journal")
	if u.RetractionNote != "статья отозвана (retraction)" {
		t.Errorf("note = %q", u.RetractionNote)
	}
	a.RetractionNote = "custom note"
	u = ApplyEligibility(a, "openalex", "Some Journal")
	if u.RetractionNote != "custom note" {
		t.Errorf("existing note must be preserved, got %q", u.RetractionNote)
	}
}

func TestScanTextTruncatesFullText(t *testing.T) {
	a := baseArticle()
	long := make([]byte, 6000)
	for i := range long {
		long[i] = 'x'
	}
	a.FullText = string(long) + " peer reviewed"
	a.PeerReviewEvidence = ""
	a.Abstract = ""
	d := EvaluateEligibility(a, "zenodo", "Some Journal")
	if d.PeerReviewed {
		t.Fatal("keyword beyond 5000 chars must not match")
	}
}
