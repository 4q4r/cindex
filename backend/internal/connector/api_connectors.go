package connector

import (
	"context"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"
	"unicode"
)

// API connector tier evidence constants (parity with api_connectors.py).
const (
	tierA = "TIER_A"
	tierB = "TIER_B"
)

// Connector executes one source fetch.
type Connector interface {
	Key() string
	// Fetch returns raw articles for the query (limit = per-source limit).
	Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error)
	// EnrichRaw runs the landing-page enrichment hook (may return nil to drop).
	EnrichRaw(ctx context.Context, raw RawArticle) (*RawArticle, error)
}

// apiBase implements shared helpers for API connectors.
type apiBase struct {
	key       string
	transport *Transport
	browser   *BrowserTransport
}

func (b *apiBase) Key() string { return b.key }

// EnrichRaw returns the raw record unchanged (API connectors provide complete
// metadata — parity with AsyncApiConnector).
func (b *apiBase) EnrichRaw(_ context.Context, raw RawArticle) (*RawArticle, error) {
	return &raw, nil
}

// extractDOIFrom returns rec fields then a text fallback.
func (b *apiBase) doiOr(text string, fields ...string) string {
	for _, f := range fields {
		if f != "" {
			return f
		}
	}
	return ExtractDOI(text)
}

// ---------------------------------------------------------------------------
// Europe PMC (+ PMC with open-access filter).
// ---------------------------------------------------------------------------

type europePMCRecord struct {
	ID                   string `json:"id"`
	PMCID                string `json:"pmcid"`
	Title                string `json:"title"`
	AbstractText         string `json:"abstractText"`
	DOI                  string `json:"doi"`
	PubYear              string `json:"pubYear"`
	FirstPublicationDate string `json:"firstPublicationDate"`
	CitedByCount         int    `json:"citedByCount"`
	JournalInfo          struct {
		Journal struct {
			Title string `json:"title"`
		} `json:"journal"`
	} `json:"journalInfo"`
	AuthorList struct {
		Author []struct {
			FullName  string `json:"fullName"`
			FirstName string `json:"firstName"`
			LastName  string `json:"lastName"`
		} `json:"author"`
	} `json:"authorList"`
	AuthorString string `json:"authorString"`
	PubTypeList  struct {
		PubType []string `json:"pubType"`
	} `json:"pubTypeList"`
	FullTextURLList struct {
		FullTextURL []struct {
			URL string `json:"url"`
		} `json:"fullTextUrl"`
	} `json:"fullTextUrlList"`
}

type europePMCResult struct {
	ResultList struct {
		Result []europePMCRecord `json:"result"`
	} `json:"resultList"`
}

func epmcAuthors(r *europePMCRecord) []string {
	var out []string
	for _, a := range r.AuthorList.Author {
		name := a.FullName
		if name == "" {
			name = strings.TrimSpace(a.FirstName + " " + a.LastName)
		}
		if name != "" {
			out = append(out, name)
		}
	}
	if len(out) == 0 && r.AuthorString != "" {
		out = SplitAuthors(r.AuthorString)
	}
	return out
}

func epmcJournal(r *europePMCRecord) string {
	if j := r.JournalInfo.Journal.Title; j != "" {
		return j
	}
	return "Europe PMC"
}

// epmcTierEvidence mirrors _epmc_tier_evidence.
func epmcTierEvidence(pubTypes []string) (peer, preprint string) {
	lowered := make([]string, 0, len(pubTypes))
	for _, t := range pubTypes {
		lowered = append(lowered, strings.ToLower(t))
	}
	for _, t := range lowered {
		if strings.Contains(t, "preprint") {
			return "", tierA + " Europe PMC: preprint"
		}
	}
	for _, t := range lowered {
		if t == "journal article" || t == "research-article" {
			return tierA + " Europe PMC: Journal Article", ""
		}
	}
	return "", ""
}

// epmcRetracted mirrors the EuropePMC/PMC retraction check: any pubType
// containing "retracted" flags the work itself ("Retraction of Publication"
// marks a notice about another work and must not flag this record).
func epmcRetracted(pubTypes []string) bool {
	for _, t := range pubTypes {
		if strings.Contains(strings.ToLower(t), "retracted") {
			return true
		}
	}
	return false
}

// pmcYear mirrors _extract_pmc_year: pubYear, then firstPublicationDate.
func pmcYear(r *europePMCRecord) int {
	if y := ToInt(r.PubYear); y > 0 {
		return y
	}
	if m := yearPattern.FindString(r.FirstPublicationDate); m != "" {
		return ExtractYear(m)
	}
	return 0
}

func (c *europePMC) build(r *europePMCRecord) *RawArticle {
	title := strings.TrimSpace(r.Title)
	if title == "" {
		return nil
	}
	abstract := strings.TrimSpace(r.AbstractText)
	if abstract == "" {
		return nil // editorial records have null abstracts; dropped (parity)
	}
	urlValue := ""
	if len(r.FullTextURLList.FullTextURL) > 0 {
		urlValue = r.FullTextURLList.FullTextURL[0].URL
	} else {
		urlValue = r.ID
	}
	if urlValue == "" {
		return nil
	}
	blob := title + " " + abstract
	raw := &RawArticle{
		SourceKey: c.key,
		Title:     title,
		Abstract:  abstract,
		DOI:       c.doiOr(blob, r.DOI),
		Journal:   epmcJournal(r),
		Authors:   epmcAuthors(r),
		Language:  "en",
		URL:       urlValue,
	}
	if y := ToInt(r.PubYear); y > 0 {
		raw.Year = &y
	}
	peer, preprint := epmcTierEvidence(r.PubTypeList.PubType)
	raw.PeerReviewEvidence = peer
	raw.PreprintEvidence = preprint
	raw.IndexingEvidence = tierB + " Europe PMC (MEDLINE/PubMed)"
	raw.CitedByCount = r.CitedByCount
	raw.IsRetracted = epmcRetracted(r.PubTypeList.PubType)
	if raw.PreprintEvidence != "" {
		raw.PeerReviewEvidence = ""
	}
	return raw
}

type europePMC struct {
	apiBase
	openAccessOnly bool
}

func (c *europePMC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s?query=%s&format=json&pageSize=%d&resultType=core",
		c.transport.apiURL(c.key), quotePlus(query), limit*3)
	if c.openAccessOnly {
		u += "%20AND%20OPEN_ACCESS:y"
	}
	var res europePMCResult
	if err := c.transport.GetJSON(ctx, c.key, u, nil, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.ResultList.Result {
		r := &res.ResultList.Result[i]
		if !c.openAccessOnly {
			if raw := c.build(r); raw != nil {
				out = append(out, *raw)
			}
			continue
		}
		// PMC: published-article version of MEDLINE records.
		title := strings.TrimSpace(r.Title)
		abstract := strings.TrimSpace(r.AbstractText)
		if title == "" || abstract == "" {
			continue
		}
		// Strip conference-abstract numbers from the title (parity with
		// _clean_pmc_title: only for pubTypes containing Abstract/Congress).
		title = cleanPMCTitle(title, r.PubTypeList.PubType)
		urlValue := ""
		if r.PMCID != "" {
			urlValue = "https://www.ncbi.nlm.nih.gov/pmc/articles/" + r.PMCID + "/"
		} else if len(r.FullTextURLList.FullTextURL) > 0 {
			urlValue = r.FullTextURLList.FullTextURL[0].URL
		}
		if urlValue == "" {
			continue
		}
		blob := title + " " + abstract
		raw := &RawArticle{
			SourceKey: c.key,
			Title:     title,
			Abstract:  abstract,
			DOI:       c.doiOr(blob, r.DOI),
			Journal:   epmcJournal(r),
			Authors:   epmcAuthors(r),
			Language:  "en",
			URL:       urlValue,
		}
		if y := pmcYear(r); y > 0 {
			raw.Year = &y
		}
		peer, preprint := epmcTierEvidence(r.PubTypeList.PubType)
		if preprint != "" {
			raw.PreprintEvidence = preprint
			raw.PeerReviewEvidence = ""
		} else if peer != "" {
			raw.PeerReviewEvidence = peer
		} else {
			raw.PeerReviewEvidence = tierB + " PubMed Central (published article)"
		}
		raw.IndexingEvidence = tierB + " pmc pubmed central"
		raw.IsRetracted = epmcRetracted(r.PubTypeList.PubType)
		raw.CitedByCount = r.CitedByCount
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// OpenAlex (+ Medknow with host filter).
// ---------------------------------------------------------------------------

type openAlexRecord struct {
	ID              string `json:"id"`
	DisplayName     string `json:"display_name"`
	Title           string `json:"title"`
	DOI             string `json:"doi"`
	Type            string `json:"type"`
	IsRetracted     bool   `json:"is_retracted"`
	CitedByCount    int    `json:"cited_by_count"`
	PublicationDate string `json:"publication_date"`
	PublicationYear int    `json:"publication_year"`
	Language        string `json:"language"`
	PrimaryLocation struct {
		LandingPageURL string `json:"landing_page_url"`
		PDFURL         string `json:"pdf_url"`
		Source         *struct {
			DisplayName string `json:"display_name"`
			Type        string `json:"type"`
		} `json:"source"`
	} `json:"primary_location"`
	BestOALocation *struct {
		Source *struct {
			DisplayName string `json:"display_name"`
			Type        string `json:"type"`
		} `json:"source"`
	} `json:"best_oa_location"`
	Authorships []struct {
		Author struct {
			DisplayName string `json:"display_name"`
		} `json:"author"`
	} `json:"authorships"`
	AbstractInvertedIndex map[string][]int `json:"abstract_inverted_index"`
}

type openAlexResult struct {
	Results []openAlexRecord `json:"results"`
}

func rebuildInvertedIndex(index map[string][]int) string {
	if len(index) == 0 {
		return ""
	}
	words := make(map[int]string)
	max := 0
	for word, positions := range index {
		for _, p := range positions {
			words[p] = word
			if p > max {
				max = p
			}
		}
	}
	var parts []string
	for i := 0; i <= max; i++ {
		if w, ok := words[i]; ok {
			parts = append(parts, w)
		}
	}
	return strings.Join(parts, " ")
}

// buildOpenAlex mirrors the OpenAlexConnector record build: landing_page_url
// or rec.id URL, empty abstracts dropped, doi from the abstract text, strict
// YYYY-MM-DD year, tier evidence from type/venue.
func (c *openalex) buildOpenAlex(r *openAlexRecord) *RawArticle {
	title := strings.TrimSpace(r.Title)
	if title == "" {
		title = strings.TrimSpace(r.DisplayName)
	}
	if title == "" {
		return nil
	}
	abstract := strings.TrimSpace(rebuildInvertedIndex(r.AbstractInvertedIndex))
	if abstract == "" {
		return nil // null-abstract works are garbage for the index (parity)
	}
	doi := strings.ReplaceAll(r.DOI, "https://doi.org/", "")
	if doi == "" {
		doi = ExtractDOI(abstract)
	}
	urlText := r.PrimaryLocation.LandingPageURL
	if urlText == "" {
		urlText = r.ID
	}
	raw := &RawArticle{
		SourceKey:    c.key,
		Title:        title,
		Abstract:     abstract,
		FullText:     strings.Join([]string{title, abstract}, " "),
		DOI:          doi,
		URL:          urlText,
		Journal:      openAlexJournal(r),
		Language:     "en",
		IsRetracted:  r.IsRetracted,
		CitedByCount: r.CitedByCount,
	}
	for _, a := range r.Authorships {
		if n := strings.TrimSpace(a.Author.DisplayName); n != "" {
			raw.Authors = append(raw.Authors, n)
		}
	}
	if y := strictYear(r.PublicationDate); y > 0 {
		raw.Year = &y
	}
	switch r.Type {
	case "preprint":
		raw.PreprintEvidence = tierA + " OpenAlex: preprint"
	case "article":
		if r.PrimaryLocation.Source != nil && r.PrimaryLocation.Source.Type == "journal" {
			raw.PeerReviewEvidence = tierB + " OpenAlex: journal article"
		}
	}
	raw.IndexingEvidence = tierB + " OpenAlex"
	return raw
}

// buildMedknow mirrors base._build_openalex_record for the Medknow host-filter
// fetch: landing_page_url → pdf_url → doi url, empty abstracts kept, journal
// default "Medknow", gated by _is_article_like_item.
func (c *openalex) buildMedknow(r *openAlexRecord) *RawArticle {
	title := strings.TrimSpace(r.DisplayName)
	if title == "" {
		title = strings.TrimSpace(r.Title)
	}
	doiURL := strings.TrimSpace(r.DOI)
	doi := strings.TrimPrefix(strings.TrimPrefix(doiURL, "https://doi.org/"), "http://doi.org/")
	urlText := r.PrimaryLocation.LandingPageURL
	if urlText == "" {
		urlText = r.PrimaryLocation.PDFURL
	}
	if urlText == "" {
		urlText = doiURL
	}
	journal := "Medknow"
	if r.PrimaryLocation.Source != nil && r.PrimaryLocation.Source.DisplayName != "" {
		journal = r.PrimaryLocation.Source.DisplayName
	}
	abstract := strings.TrimSpace(rebuildInvertedIndex(r.AbstractInvertedIndex))
	year := r.PublicationYear
	if year <= 0 {
		year = strictYear(r.PublicationDate)
	}
	if !IsArticleLikeItem(title, urlText, doi, year) {
		return nil
	}
	raw := &RawArticle{
		SourceKey:    c.key,
		Title:        title,
		Abstract:     abstract,
		FullText:     strings.Join([]string{title, abstract, journal, r.Language}, " "),
		DOI:          doi,
		URL:          urlText,
		Journal:      journal,
		Language:     r.Language,
		IsRetracted:  r.IsRetracted,
		CitedByCount: r.CitedByCount,
	}
	for _, a := range r.Authorships {
		if n := strings.TrimSpace(a.Author.DisplayName); n != "" {
			raw.Authors = append(raw.Authors, n)
		}
	}
	if year > 0 {
		raw.Year = &year
	}
	return raw
}

// openAlexJournal mirrors _extract_journal: primary_location source name,
// then best_oa_location source name.
func openAlexJournal(r *openAlexRecord) string {
	if r.PrimaryLocation.Source != nil {
		if j := strings.TrimSpace(r.PrimaryLocation.Source.DisplayName); j != "" {
			return j
		}
	}
	if r.BestOALocation != nil && r.BestOALocation.Source != nil {
		return strings.TrimSpace(r.BestOALocation.Source.DisplayName)
	}
	return ""
}

// strictYear parses a YYYY-MM-DD publication date (Django parity: any other
// format yields no year).
func strictYear(date string) int {
	if len(date) != 10 || date[4] != '-' || date[7] != '-' {
		return 0
	}
	y := ExtractYear(date)
	if y == 0 {
		return 0
	}
	if date[:4] != fmt.Sprintf("%04d", y) {
		return 0
	}
	return y
}

type openalex struct {
	apiBase
	hostFilter string
}

func (c *openalex) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	var u string
	build := c.buildOpenAlex
	if c.hostFilter != "" {
		u = fmt.Sprintf("%s?filter=primary_location.source.host_organization:%s&search=%s&per-page=%d",
			c.transport.apiURL(c.key), c.hostFilter, quotePlus(query), maxInt(3, limit*2))
		build = c.buildMedknow
	} else {
		u = fmt.Sprintf("%s?search=%s&per-page=%d", c.transport.apiURL(c.key), quotePlus(query), limit*3)
	}
	var res openAlexResult
	if err := c.transport.GetJSON(ctx, c.key, u, nil, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.Results {
		if raw := build(&res.Results[i]); raw != nil {
			out = append(out, *raw)
			if len(out) >= limit {
				break
			}
		}
	}
	return out, nil
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// ---------------------------------------------------------------------------
// Crossref.
// ---------------------------------------------------------------------------

type crossrefItem struct {
	Title               []string `json:"title"`
	Abstract            string   `json:"abstract"`
	DOI                 string   `json:"DOI"`
	Type                string   `json:"type"`
	ContainerTitle      []string `json:"container-title"`
	Volume              string   `json:"volume"`
	Issue               string   `json:"issue"`
	Page                string   `json:"page"`
	IsReferencedByCount int      `json:"is-referenced-by-count"`
	PublishedPrint      struct {
		DateParts [][]int `json:"date-parts"`
	} `json:"published-print"`
	PublishedOnline struct {
		DateParts [][]int `json:"date-parts"`
	} `json:"published-online"`
	Author []struct {
		Given  string `json:"given"`
		Family string `json:"family"`
	} `json:"author"`
	Assertion []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"assertion"`
	Relation map[string][]struct {
		ID            string `json:"id"`
		AssertionType string `json:"assertion-type"`
		Assertion     string `json:"assertion"`
	} `json:"relation"`
}

type crossrefResult struct {
	Message struct {
		Items []crossrefItem `json:"items"`
	} `json:"message"`
}

func (c *crossrefC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s?query=%s&filter=has-abstract:true&rows=%d",
		c.transport.apiURL(c.key), quotePlus(query), limit*3)
	var res crossrefResult
	if err := c.transport.GetJSON(ctx, c.key, u, nil, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.Message.Items {
		item := &res.Message.Items[i]
		title := ""
		if len(item.Title) > 0 {
			title = strings.TrimSpace(item.Title[0])
		}
		if title == "" {
			continue
		}
		abstract := strings.TrimSpace(item.Abstract)
		doi := c.doiOr(title+" "+item.Abstract, item.DOI)
		if doi == "" {
			continue
		}
		raw := &RawArticle{
			SourceKey:    c.key,
			Title:        title,
			Abstract:     abstract,
			FullText:     strings.Join([]string{title, abstract}, " "),
			DOI:          doi,
			URL:          "https://doi.org/" + doi,
			Journal:      firstString(item.ContainerTitle),
			Volume:       item.Volume,
			Issue:        item.Issue,
			Pages:        item.Page,
			Language:     "en",
			CitedByCount: item.IsReferencedByCount,
		}
		if abstract == "" {
			continue // book front-matter/back-matter records (parity)
		}
		for _, a := range item.Author {
			name := strings.TrimSpace(a.Given + " " + a.Family)
			if name != "" {
				raw.Authors = append(raw.Authors, name)
			}
		}
		if dp := crossrefDateParts(&item.PublishedPrint); len(dp) > 0 {
			if y := dp[0]; y > 0 {
				raw.Year = &y
			}
		}
		if raw.Year == nil {
			if dp := crossrefDateParts(&item.PublishedOnline); len(dp) > 0 && dp[0] > 0 {
				raw.Year = &dp[0]
			}
		}
		switch item.Type {
		case "posted-content":
			raw.PreprintEvidence = tierA + " Crossref: posted-content (preprint)"
			raw.PeerReviewEvidence = ""
		case "journal-article":
			hasAccepted := false
			for _, as := range item.Assertion {
				if as.Name == "received" || as.Name == "accepted" {
					hasAccepted = true
				}
			}
			if hasAccepted {
				raw.PeerReviewEvidence = tierA + " Crossref: received/accepted assertion"
			} else {
				raw.PeerReviewEvidence = tierB + " Crossref: journal-article"
			}
		}
		raw.IndexingEvidence = tierB + " Crossref (DOI registry)"
		raw.IsRetracted, raw.RetractionNote = crossrefRetraction(item)
		out = append(out, *raw)
	}
	return out, nil
}

// crossrefRetraction mirrors _crossref_retraction: an assertion named
// retraction, a relation.retraction id, or an is-update-to relation whose
// assertion-type mentions a retraction.
func crossrefRetraction(item *crossrefItem) (bool, string) {
	for _, a := range item.Assertion {
		if strings.Contains(strings.ToLower(a.Name), "retract") {
			return true, strings.TrimSpace(a.Value)
		}
	}
	for _, rel := range item.Relation["retraction"] {
		return true, strings.TrimSpace(rel.ID)
	}
	for _, rel := range item.Relation["is-update-to"] {
		if strings.Contains(strings.ToLower(rel.AssertionType), "retract") {
			return true, strings.TrimSpace(rel.ID)
		}
	}
	return false, ""
}

func crossrefDateParts(p *struct {
	DateParts [][]int `json:"date-parts"`
}) []int {
	if p != nil && len(p.DateParts) > 0 && len(p.DateParts[0]) > 0 {
		return p.DateParts[0]
	}
	return nil
}

func firstString(s []string) string {
	if len(s) > 0 {
		return strings.TrimSpace(s[0])
	}
	return ""
}

type crossrefC struct{ apiBase }

// ---------------------------------------------------------------------------
// PubMed (esearch + esummary + efetch).
// ---------------------------------------------------------------------------

type pubmedSearch struct {
	ESearchResult struct {
		IDList []string `json:"idlist"`
	} `json:"esearchresult"`
}

type pubmedSummary struct {
	Result map[string]struct {
		UID             string `json:"uid"`
		Title           string `json:"title"`
		Pubdate         string `json:"pubdate"`
		FullJournalName string `json:"fulljournalname"`
		Source          string `json:"source"`
		ArticleIDs      []struct {
			IDType string `json:"idtype"`
			Value  string `json:"value"`
		} `json:"articleids"`
		Authors []struct {
			Name string `json:"name"`
		} `json:"authors"`
	} `json:"result"`
}

type pubmedArticle struct {
	PubmedArticle []struct {
		MedlineCitation struct {
			Article struct {
				Abstract struct {
					AbstractText []struct {
						Label string `xml:"Label,attr"`
						Value string `xml:",chardata"`
					} `xml:"AbstractText"`
				} `xml:"Abstract"`
			} `xml:"Article"`
		} `xml:"MedlineCitation"`
	} `xml:"PubmedArticle"`
}

type pubmed struct{ apiBase }

func (c *pubmed) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	base := c.transport.apiURL(c.key)
	searchURL := fmt.Sprintf("%s/esearch.fcgi?db=pubmed&term=%s&retmax=%d&retmode=json",
		base, quotePlus(query), limit)
	var sres pubmedSearch
	if err := c.transport.GetJSON(ctx, c.key, searchURL, nil, &sres); err != nil {
		return nil, err
	}
	ids := sres.ESearchResult.IDList
	if len(ids) == 0 {
		return nil, nil
	}
	sumURL := fmt.Sprintf("%s/esummary.fcgi?db=pubmed&id=%s&retmode=json",
		base, strings.Join(ids, ","))
	var sum pubmedSummary
	if err := c.transport.GetJSON(ctx, c.key, sumURL, nil, &sum); err != nil {
		return nil, err
	}
	abstracts := map[string]string{}
	efetchURL := fmt.Sprintf("%s/efetch.fcgi?db=pubmed&id=%s&retmode=xml", base, strings.Join(ids, ","))
	if body, err := c.transport.GetText(ctx, c.key, efetchURL, ""); err == nil {
		var pa pubmedArticle
		if xml.Unmarshal([]byte(body), &pa) == nil {
			for i, a := range pa.PubmedArticle {
				if i >= len(ids) {
					break
				}
				var parts []string
				for _, t := range a.MedlineCitation.Article.Abstract.AbstractText {
					if t.Label != "" {
						parts = append(parts, t.Label+": "+t.Value)
					} else {
						parts = append(parts, t.Value)
					}
				}
				if len(parts) > 0 {
					abstracts[ids[i]] = strings.Join(parts, " ")
				}
			}
		}
	}
	var out []RawArticle
	for _, uid := range ids {
		rec, ok := sum.Result[uid]
		if !ok {
			continue
		}
		title := strings.TrimSpace(rec.Title)
		if title == "" {
			continue
		}
		doi := ""
		for _, aid := range rec.ArticleIDs {
			if aid.IDType == "doi" {
				doi = aid.Value
			}
		}
		if doi == "" {
			doi = ExtractDOI(title)
		}
		journal := rec.FullJournalName
		if journal == "" {
			journal = rec.Source
		}
		raw := &RawArticle{
			SourceKey:          c.key,
			Title:              title,
			DOI:                doi,
			URL:                "https://pubmed.ncbi.nlm.nih.gov/" + uid + "/",
			Journal:            journal,
			Language:           "en",
			Abstract:           abstracts[uid],
			FullText:           fullTextFor(title, abstracts[uid]),
			PeerReviewEvidence: tierB + " MEDLINE-indexed (PubMed)",
			IndexingEvidence:   tierB + " medline pubmed",
		}
		if f := strings.Fields(rec.Pubdate); len(f) > 0 {
			if y := ToInt(f[0]); y > 0 {
				raw.Year = &y
			}
		}
		for _, a := range rec.Authors {
			if n := strings.TrimSpace(a.Name); n != "" {
				raw.Authors = append(raw.Authors, n)
			}
		}
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// arXiv (Atom XML).
// ---------------------------------------------------------------------------

type arxivFeed struct {
	Entries []struct {
		Title     string `xml:"title"`
		Summary   string `xml:"summary"`
		Published string `xml:"published"`
		ID        string `xml:"id"`
		Authors   []struct {
			Name string `xml:"name"`
		} `xml:"author"`
	} `xml:"entry"`
}

type arxivC struct{ apiBase }

func (c *arxivC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s?search_query=all:%s&start=0&max_results=%d",
		c.transport.apiURL(c.key), quotePlus(query), limit)
	body, err := c.transport.GetText(ctx, c.key, u, "")
	if err != nil {
		return nil, err
	}
	var feed arxivFeed
	if err := xml.Unmarshal([]byte(body), &feed); err != nil {
		return nil, fetchErr(c.key, "invalid arXiv XML: %v", err)
	}
	var out []RawArticle
	for _, e := range feed.Entries {
		title := strings.TrimSpace(e.Title)
		if title == "" {
			continue
		}
		arxivID := strings.TrimSpace(e.ID)
		doi := ""
		if strings.Contains(arxivID, "arxiv.org/abs/") {
			arxivID = arxivID[strings.Index(arxivID, "arxiv.org/abs/")+len("arxiv.org/abs/"):]
			doi = "10.48550/arXiv." + arxivID
		}
		if d := ExtractDOI(title + " " + e.Summary); d != "" {
			doi = d
		}
		raw := &RawArticle{
			SourceKey:        c.key,
			Title:            title,
			Abstract:         strings.TrimSpace(e.Summary),
			DOI:              doi,
			URL:              strings.TrimSpace(e.ID),
			Journal:          "arXiv",
			Language:         "en",
			PreprintEvidence: tierA + " arXiv preprint",
		}
		if y := ExtractYear(e.Published); y > 0 {
			raw.Year = &y
		}
		for _, a := range e.Authors {
			if n := strings.TrimSpace(a.Name); n != "" {
				raw.Authors = append(raw.Authors, n)
			}
		}
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// DOAJ.
// ---------------------------------------------------------------------------

type doajResult struct {
	Results []struct {
		BibJSON struct {
			Title      string `json:"title"`
			Abstract   string `json:"abstract"`
			Year       string `json:"year"`
			Identifier []struct {
				Type string `json:"type"`
				ID   string `json:"id"`
			} `json:"identifier"`
			Link []struct {
				Type string `json:"type"`
				URL  string `json:"url"`
			} `json:"link"`
			Author []struct {
				Name string `json:"name"`
			} `json:"author"`
			Journal struct {
				Title string `json:"title"`
			} `json:"journal"`
		} `json:"bibjson"`
	} `json:"results"`
}

var doajLabelRe = regexp.MustCompile(`(?i)^\s*abstract\s*(?:[:：]\s*|\s+)`)

// stripDoajAbstractLabel mirrors DOAJConnector._strip_abstract_label: the
// leading Abstract label is removed only when followed by a colon or by a
// capitalised token, so "Abstract algebra is..." is preserved verbatim.
func stripDoajAbstractLabel(abstract string) string {
	if abstract == "" {
		return abstract
	}
	m := doajLabelRe.FindStringIndex(abstract)
	if m == nil {
		return abstract
	}
	remainder := abstract[m[1]:]
	matched := abstract[m[0]:m[1]]
	hadColon := strings.Contains(matched, ":") || strings.Contains(matched, "：")
	if hadColon || (remainder != "" && unicode.IsUpper([]rune(remainder)[0])) {
		return remainder
	}
	return abstract
}

type doajC struct{ apiBase }

func (c *doajC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s/%s?pageSize=%d", c.transport.apiURL(c.key), quotePlus(query), limit)
	var res doajResult
	if err := c.transport.GetJSON(ctx, c.key, u, nil, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.Results {
		bib := &res.Results[i].BibJSON
		title := strings.TrimSpace(bib.Title)
		if title == "" {
			continue
		}
		doi := ""
		for _, id := range bib.Identifier {
			if id.Type == "doi" {
				doi = id.ID
			}
		}
		if doi == "" {
			doi = ExtractDOI(title + " " + bib.Abstract)
		}
		link := ""
		for _, l := range bib.Link {
			if l.Type == "fulltext" {
				link = l.URL
				break
			}
			if link == "" {
				link = l.URL
			}
		}
		if link == "" && doi != "" {
			link = "https://doi.org/" + doi
		}
		raw := &RawArticle{
			SourceKey:          c.key,
			Title:              title,
			Abstract:           stripDoajAbstractLabel(bib.Abstract),
			FullText:           fullTextFor(title, bib.Abstract),
			DOI:                doi,
			URL:                link,
			Journal:            strings.TrimSpace(bib.Journal.Title),
			Language:           "en",
			PeerReviewEvidence: tierB + " DOAJ journal (peer-reviewed by policy)",
			IndexingEvidence:   tierB + " doaj",
		}
		if y := ToInt(bib.Year); y > 0 {
			raw.Year = &y
		}
		for _, a := range bib.Author {
			if n := strings.TrimSpace(a.Name); n != "" {
				raw.Authors = append(raw.Authors, n)
			}
		}
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// CORE.
// ---------------------------------------------------------------------------

type coreResult struct {
	Results []struct {
		Title             string `json:"title"`
		Abstract          string `json:"abstract"`
		DOI               string `json:"doi"`
		DownloadURL       string `json:"downloadUrl"`
		SourceFulltextURL string `json:"sourceFulltextUrl"`
		YearPublished     string `json:"yearPublished"`
		Publisher         string `json:"publisher"`
		Authors           []struct {
			Name string `json:"name"`
		} `json:"authors"`
	} `json:"results"`
}

type coreC struct {
	apiBase
	apiKey string
}

func (c *coreC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s?q=%s&limit=%d", c.transport.apiURL(c.key), quotePlus(query), limit)
	headers := map[string]string{}
	if c.apiKey != "" {
		headers["Authorization"] = "Bearer " + c.apiKey
	}
	var res coreResult
	if err := c.transport.GetJSON(ctx, c.key, u, headers, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.Results {
		r := &res.Results[i]
		title := strings.TrimSpace(r.Title)
		if title == "" {
			continue
		}
		link := r.DownloadURL
		if link == "" {
			link = r.SourceFulltextURL
		}
		if link == "" && r.DOI != "" {
			link = "https://doi.org/" + r.DOI
		}
		if link == "" {
			continue // no usable URL (parity)
		}
		raw := &RawArticle{
			SourceKey: c.key,
			Title:     title,
			Abstract:  strings.TrimSpace(r.Abstract),
			FullText:  fullTextFor(title, r.Abstract),
			DOI:       c.doiOr(title+" "+r.Abstract, r.DOI),
			URL:       link,
			Journal:   strings.TrimSpace(r.Publisher),
			Language:  "en",
		}
		if y := ToInt(r.YearPublished); y > 0 {
			raw.Year = &y
		}
		for _, a := range r.Authors {
			if n := strings.TrimSpace(a.Name); n != "" {
				raw.Authors = append(raw.Authors, n)
			}
		}
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// DBLP.
// ---------------------------------------------------------------------------

type dblpResult struct {
	Result struct {
		Hits struct {
			Hit []struct {
				Info struct {
					Title   string `json:"title"`
					URL     string `json:"url"`
					EE      string `json:"ee"`
					DOI     string `json:"doi"`
					Year    string `json:"year"`
					Venue   string `json:"venue"`
					Authors struct {
						Author []json.RawMessage `json:"author"`
					} `json:"authors"`
				} `json:"info"`
			} `json:"hit"`
		} `json:"hits"`
	} `json:"result"`
}

type dblpC struct{ apiBase }

func (c *dblpC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s?q=%s&format=json&h=%d", c.transport.apiURL(c.key), quotePlus(query), limit)
	var res dblpResult
	if err := c.transport.GetJSON(ctx, c.key, u, nil, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.Result.Hits.Hit {
		info := &res.Result.Hits.Hit[i].Info
		title := strings.TrimSpace(info.Title)
		if title == "" {
			continue
		}
		link := info.URL
		if link == "" {
			link = info.EE
		}
		doi := info.DOI
		if doi == "" {
			doi = ExtractDOI(title)
		}
		if doi == "" {
			doi = ExtractDOI(link)
		}
		if link == "" && doi != "" {
			link = "https://doi.org/" + doi
		}
		if link == "" {
			continue // no usable URL (parity)
		}
		raw := &RawArticle{
			SourceKey: c.key,
			Title:     title,
			DOI:       doi,
			URL:       link,
			Journal:   strings.TrimSpace(info.Venue),
			Language:  "en",
			FullText:  title,
		}
		if y := ToInt(info.Year); y > 0 {
			raw.Year = &y
		}
		for _, a := range info.Authors.Author {
			var s string
			if json.Unmarshal(a, &s) == nil {
				if s = strings.TrimSpace(s); s != "" {
					raw.Authors = append(raw.Authors, s)
				}
				continue
			}
			var obj struct {
				Text string `json:"text"`
			}
			if json.Unmarshal(a, &obj) == nil && obj.Text != "" {
				raw.Authors = append(raw.Authors, strings.TrimSpace(obj.Text))
			}
		}
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// HAL (Solr).
// ---------------------------------------------------------------------------

type halResult struct {
	Response struct {
		Docs []struct {
			Title           []string `json:"title_s"`
			Abstract        []string `json:"abstract_s"`
			DOI             []string `json:"doiId_s"`
			URI             []string `json:"uri_s"`
			PublicationYear int      `json:"publicationDateY_i"`
			AuthFullName    []string `json:"authFullName_s"`
			JournalTitle    []string `json:"journalTitle_s"`
			Language        []string `json:"language_s"`
			PeerReviewing   string   `json:"peerReviewing_s"`
			DocType         string   `json:"docType_s"`
		} `json:"docs"`
	} `json:"response"`
}

type halC struct{ apiBase }

func (c *halC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s?q=%s&fl=halId_s,title_s,authFullName_s,abstract_s,doiId_s,publicationDateY_i,uri_s,journalTitle_s,language_s,peerReviewing_s,docType_s&rows=%d&wt=json&sort=score desc",
		c.transport.apiURL(c.key), quotePlus(query), limit)
	var res halResult
	if err := c.transport.GetJSON(ctx, c.key, u, nil, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.Response.Docs {
		d := &res.Response.Docs[i]
		title := firstString(d.Title)
		if title == "" {
			continue
		}
		journal := firstString(d.JournalTitle)
		if len([]rune(journal)) < 3 {
			journal = "HAL"
		}
		link := firstString(d.URI)
		raw := &RawArticle{
			SourceKey: c.key,
			Title:     title,
			Abstract:  firstString(d.Abstract),
			DOI:       c.doiOr(title, firstString(d.DOI)),
			URL:       link,
			Journal:   journal,
			Language:  firstString(d.Language),
			Authors:   d.AuthFullName,
		}
		if raw.Language == "" {
			raw.Language = "fr"
		}
		if raw.DOI != "" && raw.URL == "" {
			raw.URL = "https://doi.org/" + raw.DOI
		}
		if d.PublicationYear > 0 {
			raw.Year = &d.PublicationYear
		}
		if d.PeerReviewing == "1" {
			raw.PeerReviewEvidence = tierA + " HAL: peerReviewing=1"
		}
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// Zenodo.
// ---------------------------------------------------------------------------

type zenodoResult struct {
	Hits struct {
		Hits []struct {
			ID       string `json:"id"`
			Metadata struct {
				Title           string `json:"title"`
				Description     string `json:"description"`
				DOI             string `json:"doi"`
				PublicationDate string `json:"publication_date"`
				Creators        []struct {
					Name string `json:"name"`
				} `json:"creators"`
				Journal struct {
					Title string `json:"title"`
				} `json:"journal"`
			} `json:"metadata"`
		} `json:"hits"`
	} `json:"hits"`
}

type zenodoC struct{ apiBase }

func (c *zenodoC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	u := fmt.Sprintf("%s?q=%s&size=%d&sort=mostrecent&type=publication&subtype=article",
		c.transport.apiURL(c.key), quotePlus(query), limit)
	var res zenodoResult
	if err := c.transport.GetJSON(ctx, c.key, u, nil, &res); err != nil {
		return nil, err
	}
	var out []RawArticle
	for i := range res.Hits.Hits {
		h := &res.Hits.Hits[i]
		meta := &h.Metadata
		title := strings.TrimSpace(meta.Title)
		if title == "" {
			continue
		}
		doi := meta.DOI
		if doi == "" {
			doi = ExtractDOI(title + " " + meta.Description)
		}
		link := "https://zenodo.org/record/" + h.ID
		if doi != "" {
			link = "https://doi.org/" + doi
		}
		raw := &RawArticle{
			SourceKey: c.key,
			Title:     title,
			Abstract:  strings.TrimSpace(meta.Description),
			DOI:       doi,
			URL:       link,
			Journal:   strings.TrimSpace(meta.Journal.Title),
			Language:  "en",
		}
		if y := ExtractYear(meta.PublicationDate); y > 0 {
			raw.Year = &y
		}
		for _, cr := range meta.Creators {
			if n := strings.TrimSpace(cr.Name); n != "" {
				raw.Authors = append(raw.Authors, n)
			}
		}
		out = append(out, *raw)
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// IACR (RSS through the sidecar; no DOI).
// ---------------------------------------------------------------------------

type iacrRSS struct {
	XMLName xml.Name `xml:"rss"`
	Items   []struct {
		Title       string   `xml:"title"`
		Link        string   `xml:"link"`
		Description string   `xml:"description"`
		PubDate     string   `xml:"pubDate"`
		Creators    []string `xml:"http://purl.org/dc/elements/1.1/ creator"`
	} `xml:"channel>item"`
}

type iacrC struct{ apiBase }

func (c *iacrC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	page, err := c.browser.Fetch(ctx, c.key, c.transport.apiURL(c.key), nil,
		"application/rss+xml,application/xml,text/xml,*/*", 25)
	if err != nil {
		return nil, err
	}
	var rss iacrRSS
	if err := xml.Unmarshal([]byte(page.Body), &rss); err != nil {
		return nil, fetchErr(c.key, "invalid RSS: %v", err)
	}
	if rss.XMLName.Local != "rss" && len(rss.Items) == 0 {
		return nil, fetchErr(c.key, "RSS feed body is not an RSS document (root=%q)", rss.XMLName.Local)
	}
	var out []RawArticle
	for _, item := range rss.Items {
		title := strings.TrimSpace(item.Title)
		link := strings.TrimSpace(item.Link)
		abstract := strings.TrimSpace(item.Description)
		if title == "" || !strings.HasPrefix(link, "http") {
			continue
		}
		if !MatchAllTerms(query, title, abstract) {
			continue
		}
		raw := &RawArticle{
			SourceKey:        c.key,
			Title:            title,
			Abstract:         abstract,
			URL:              link,
			Journal:          "IACR ePrint",
			Language:         "en",
			PreprintEvidence: tierA + " IACR ePrint (preprint)",
		}
		if y := ExtractYear(item.PubDate + " " + link); y > 0 {
			raw.Year = &y
		}
		for _, cr := range item.Creators {
			if n := strings.TrimSpace(cr); n != "" {
				raw.Authors = append(raw.Authors, n)
			}
		}
		out = append(out, *raw)
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// Exa.
// ---------------------------------------------------------------------------

// nonScholarlyHosts mirrors _NON_SCHOLARLY_HOSTS (exact hosts, www. stripped;
// wikipedia.org matches subdomains too).
var nonScholarlyHosts = []string{
	"developers.google.com", "mayoclinic.org", "merckmanuals.com",
	"geeksforgeeks.org", "niddk.nih.gov", "ibm.com", "research.ibm.com",
	"nist.gov", "coursera.org", "mitsloan.mit.edu", "ischoolonline.berkeley.edu",
	"cancerresearch.org", "cancertherapyadvisor.com",
	"cancerimmunotherapyconference.org", "quantum.microsoft.com", "q-ctrl.com",
	"riverlane.com", "quora.com",
}

// exaLangQueries mirrors expand_query_for_exa.
var exaLangQueries = []string{"en", "ru", "de", "fr", "es", "zh-CN", "ja", "ko"}

// exaSystemPrompt mirrors the Exa search systemPrompt verbatim.
const exaSystemPrompt = "Find scholarly articles that clearly indicate their publication status. " +
	"For each result, signal whether it is: (1) peer-reviewed or refereed — look for explicit statements like " +
	"'peer-reviewed', 'refereed journal', or journal names known for peer review; " +
	"(2) indexed in a reputable database — Scopus, Web of Science, MEDLINE, PubMed Central, DOAJ, RINC/eLibrary, KCI; " +
	"(3) has a DOI and journal citation — DOI must be present, journal name must be identifiable; " +
	"(4) is a preprint or author manuscript — preprints.org, arXiv, bioRxiv, medRxiv, SSRN, or labelled 'preprint', 'working paper', " +
	"'author manuscript'. Prefer published peer-reviewed journal articles over preprints. " +
	"Avoid duplicates and non-scholarly content."

// exaHighlightsQuery mirrors the Exa highlights query verbatim.
const exaHighlightsQuery = "peer-reviewed refereed status, journal indexing in Scopus or Web of Science, DOI, preprint vs published article"

type exaSearchBody struct {
	Query      string `json:"query"`
	Type       string `json:"type"`
	Category   string `json:"category"`
	NumResults int    `json:"num_results"`
	Contents   struct {
		Text struct {
			MaxCharacters int `json:"maxCharacters"`
		} `json:"text"`
		Highlights struct {
			Query         string `json:"query"`
			MaxCharacters int    `json:"maxCharacters"`
		} `json:"highlights"`
	} `json:"contents"`
	SystemPrompt string `json:"systemPrompt"`
}

type exaResult struct {
	Results []struct {
		Title         string   `json:"title"`
		Name          string   `json:"name"`
		URL           string   `json:"url"`
		ID            string   `json:"id"`
		Text          string   `json:"text"`
		Content       string   `json:"content"`
		Snippet       string   `json:"snippet"`
		Highlights    []string `json:"highlights"`
		Author        any      `json:"author"`
		Authors       any      `json:"authors"`
		PublishedDate string   `json:"publishedDate"`
	} `json:"results"`
}

var exaBracketRe = regexp.MustCompile(`\([^)]*\)$`)
var exaOpenBracketRe = regexp.MustCompile(`\([^)]*$`)
var exaCloseParenRe = regexp.MustCompile(`\)$`)

// exaExtractDOI mirrors ExaConnector._extract_doi (trailing paren cleanup).
func exaExtractDOI(text string) string {
	doi := ExtractDOI(text)
	if doi == "" {
		return ""
	}
	doi = strings.TrimRight(doi, ".")
	doi = exaBracketRe.ReplaceAllString(doi, "")
	doi = exaOpenBracketRe.ReplaceAllString(doi, "")
	doi = exaCloseParenRe.ReplaceAllString(doi, "")
	return strings.TrimSpace(doi)
}

// isNonScholarlyDomain mirrors _is_non_scholarly_domain: netloc parsed, www.
// stripped, wikipedia subdomains matched, other hosts exact.
func isNonScholarlyDomain(u string) bool {
	parsed, err := url.Parse(u)
	if err != nil || parsed.Hostname() == "" {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	host = strings.TrimPrefix(host, "www.")
	if host == "wikipedia.org" || strings.HasSuffix(host, ".wikipedia.org") {
		return true
	}
	for _, h := range nonScholarlyHosts {
		if host == h {
			return true
		}
	}
	return false
}

// Exa citation metadata patterns (parity with ExaConnector).
var (
	exaJournalPattern  = regexp.MustCompile(`(?:published\s+in[:\s]+([A-Z][^\n,;]{3,120}?)\s*(?:,|;|\n|\.\s)|journal\s*[:=]\s*([^\n,;]{3,120}?)\s*(?:,|;|\n|\.\s)|([A-Z][a-zA-Z\s&]+(?:Journal|Review|Letters|Annals|Archive|Proceedings|Transactions|Bulletin|Reports?|Communications|Research|Studies|Science|Medicine|Physics|Chemistry|Biology|Engineering|Computing|Informatics))\b)`)
	exaJournalDeny     = regexp.MustCompile(`(?i)\b(?:further\s+reading|related\s+articles?|see\s+also|references?|pubmed|medline|abstract|introduction|background|methods?|results?|discussion|conclusions?|acknowledg|funding|author\s+contrib|competing\s+interest|data\s+availab|ethics\s+declar|supplement|appendix|keywords?|subjects?|cite\s+this|cited\s+by|full\s+text|open\s+access|download|sign\s+in|register|this\s+article|the\s+author|articles?\s+for\s+further|scientific\s+journal\s+articles|licensing\s+statements?|publication\s+(?:history|identifier|syntax)|editorial\s+review|technical\s+series|nist\s+technical|approved\s+by|issn|isbn|doi\s+is|available\s+at|accessed)\b`)
	exaVolumePattern   = regexp.MustCompile(`(?i)\b(?:vol(?:ume)?\.?\s*(\d+)|v\.(\d+))\b`)
	exaIssuePattern    = regexp.MustCompile(`(?i)\b(?:no\.?\s*(\d+)|issue\s+(\d+)|n\.(\d+))\b`)
	exaPagesPattern    = regexp.MustCompile(`(?i)\b(?:pp?\.?\s*(\d+\s*[-–]\s*\d+)|pages?\s+(\d+\s*[-–]\s*\d+))\b`)
	exaCitationPattern = regexp.MustCompile(`(?:([A-Z][a-zA-Z\s.&]{2,80}?)\s*(?:,\s*)?((?:19|20)\d{2})\s*[;:]\s*(\d+)\s*(?:\((\d+)\)\s*)?[;:]?\s*(\d+\s*[-–]\s*\d+))`)
)

const exaJournalMaxLen = 60

func validateExaJournal(journal string) bool {
	if journal == "" {
		return false
	}
	if strings.Contains(journal, "[") || strings.Contains(journal, "]") {
		return false
	}
	if len([]rune(journal)) > exaJournalMaxLen {
		return false
	}
	return !exaJournalDeny.MatchString(journal)
}

func extractExaJournal(text string) string {
	m := exaJournalPattern.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	journal := m[1]
	if journal == "" {
		journal = m[2]
	}
	if journal == "" {
		journal = m[3]
	}
	journal = strings.TrimSpace(journal)
	if !validateExaJournal(journal) {
		return ""
	}
	return strings.TrimRight(journal, ".,;:")
}

func extractExaVolume(text string) string {
	m := exaVolumePattern.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	return m[1] + m[2]
}

func extractExaIssue(text string) string {
	m := exaIssuePattern.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	return m[1] + m[2] + m[3]
}

func extractExaPages(text string) string {
	m := exaPagesPattern.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	p := m[1] + m[2]
	return strings.ReplaceAll(p, "–", "-")
}

// exaCitationFromText mirrors _extract_citation_from_text: combined pattern
// first, then individual fallbacks.
func exaCitationFromText(text string) map[string]string {
	result := map[string]string{}
	m := exaCitationPattern.FindStringSubmatch(text)
	if m != nil {
		journal := strings.TrimSpace(m[1])
		if journal != "" {
			journal = strings.TrimRight(journal, ".,;:")
			if validateExaJournal(journal) {
				result["journal"] = journal
			}
		}
		if m[3] != "" {
			result["volume"] = strings.TrimSpace(m[3])
		}
		if m[4] != "" {
			result["issue"] = strings.TrimSpace(m[4])
		}
		if m[5] != "" {
			result["pages"] = strings.ReplaceAll(strings.TrimSpace(m[5]), "–", "-")
		}
	}
	fallbacks := map[string]string{
		"journal": extractExaJournal(text),
		"volume":  extractExaVolume(text),
		"issue":   extractExaIssue(text),
		"pages":   extractExaPages(text),
	}
	for k, v := range fallbacks {
		if _, ok := result[k]; !ok && v != "" {
			result[k] = v
		}
	}
	return result
}

// normalizeExaAuthor mirrors _normalize_author_value (str / dict / list).
func normalizeExaAuthor(author any) []string {
	switch v := author.(type) {
	case nil:
		return nil
	case string:
		cleaned := NormalizeScholarly(v, -1)
		if cleaned == "" {
			return nil
		}
		return []string{cleaned}
	case map[string]any:
		name := ""
		for _, k := range []string{"name", "full_name"} {
			if s, ok := v[k].(string); ok {
				name = strings.TrimSpace(s)
				if name != "" {
					break
				}
			}
		}
		cleaned := NormalizeScholarly(name, -1)
		if cleaned == "" {
			return nil
		}
		return []string{cleaned}
	case []any:
		var out []string
		for _, item := range v {
			out = append(out, normalizeExaAuthor(item)...)
		}
		return out
	default:
		return nil
	}
}

type exaC struct {
	apiBase
	apiKey    string
	translate func(ctx context.Context, query, lang string) string
}

func (c *exaC) Fetch(ctx context.Context, query string, limit int) ([]RawArticle, error) {
	if c.apiKey == "" {
		return nil, fetchErr(c.key, "EXA_API_KEY is required")
	}
	perLang := maxInt(3, limit/len(exaLangQueries)+2)
	seen := map[string]bool{}
	var out []RawArticle
	var lastErr error
	headers := map[string]string{
		"Accept":       "application/json",
		"Content-Type": "application/json",
		"x-api-key":    c.apiKey,
	}
	for _, lang := range exaLangQueries {
		if ctx.Err() != nil {
			break
		}
		q := query
		if c.translate != nil {
			q = c.translate(ctx, query, lang)
		}
		if q == "" {
			continue
		}
		var body exaSearchBody
		body.Query = q
		body.Type = "auto"
		body.Category = "research paper"
		body.NumResults = perLang
		body.Contents.Text.MaxCharacters = 10000
		body.Contents.Highlights.Query = exaHighlightsQuery
		body.Contents.Highlights.MaxCharacters = 3000
		body.SystemPrompt = exaSystemPrompt
		var res exaResult
		if err := c.transport.PostJSON(ctx, c.key, c.transport.apiURL(c.key), headers, &body, &res); err != nil {
			var fetchErr *FetchError
			if !errors.As(err, &fetchErr) {
				lastErr = err
			}
			continue
		}
		for i := range res.Results {
			r := &res.Results[i]
			link := r.URL
			if link == "" {
				link = r.ID
			}
			link = strings.TrimSpace(link)
			title := strings.TrimSpace(r.Title)
			if title == "" {
				title = strings.TrimSpace(r.Name)
			}
			if title == "" || link == "" {
				continue
			}
			if isNonScholarlyDomain(link) {
				continue
			}
			if seen[link] {
				continue
			}
			seen[link] = true
			text := r.Text
			if text == "" {
				text = r.Content
			}
			if text == "" {
				text = r.Snippet
			}
			abstract := CleanAbstract(text, title)
			if len([]rune(abstract)) > 8000 {
				abstract = string([]rune(abstract)[:8000])
			}
			var highlightsText string
			for _, h := range r.Highlights {
				if h != "" {
					if highlightsText != "" {
						highlightsText += " "
					}
					highlightsText += h
				}
			}
			authors := normalizeExaAuthor(r.Author)
			if len(authors) == 0 && r.Authors != nil {
				authors = normalizeExaAuthor(r.Authors)
			}
			year := ExtractYear(r.PublishedDate)
			doi := exaExtractDOI(title + " " + text + " " + link)
			citation := exaCitationFromText(title + " " + text + " " + highlightsText)
			journal := citation["journal"]
			combined := strings.Join([]string{title, text, journal, strings.Join(authors, " ")}, " ")
			if !IsArticleLikeItem(title, link, doi, year) {
				continue
			}
			evidenceSource := highlightsText + " " + combined
			peer, indexing, preprint := MergeEvidence(evidenceSource, "", "", "")
			raw := &RawArticle{
				SourceKey:          c.key,
				Title:              title,
				Abstract:           abstract,
				FullText:           combined,
				DOI:                doi,
				URL:                link,
				Journal:            journal,
				Authors:            authors,
				Volume:             citation["volume"],
				Issue:              citation["issue"],
				Pages:              citation["pages"],
				Language:           "multi",
				Year:               intPtr(year),
				PeerReviewEvidence: peer,
				IndexingEvidence:   indexing,
				PreprintEvidence:   preprint,
			}
			out = append(out, *raw)
			if len(out) >= limit {
				break
			}
		}
		if len(out) >= limit {
			break
		}
		select {
		case <-time.After(300 * time.Millisecond):
		case <-ctx.Done():
		}
	}
	if len(out) == 0 && lastErr != nil {
		return nil, fetchErr(c.key, "request failed after retries: %v", lastErr)
	}
	c.enrich(ctx, query, out)
	return out[:minInt(len(out), limit)], nil
}

func intPtr(v int) *int {
	if v == 0 {
		return nil
	}
	return &v
}

// enrich mirrors ExaConnector._apply_enrichment: a single deep-lite
// outputSchema call fills authors/year/doi/journal for items missing them.
func (c *exaC) enrich(ctx context.Context, query string, items []RawArticle) {
	var missing []*RawArticle
	for i := range items {
		item := &items[i]
		if len(item.Authors) == 0 || item.Year == nil || item.DOI == "" {
			missing = append(missing, item)
		}
	}
	if len(missing) == 0 {
		return
	}
	var urls []string
	for _, m := range missing {
		urls = append(urls, m.URL)
	}
	meta, err := c.enrichOutputSchema(ctx, urls, query)
	if err != nil {
		return // enrichment failure is non-fatal (parity)
	}
	if len(meta) == 0 {
		return
	}
	for i := range items {
		item := &items[i]
		m := meta[item.URL]
		if len(m) == 0 {
			continue
		}
		if a, ok := m["authors"].([]string); ok {
			item.Authors = a
		}
		if y, ok := m["year"].(int); ok && y > 0 {
			item.Year = intPtr(y)
		}
		if d, ok := m["doi"].(string); ok {
			item.DOI = d
		}
		if j, ok := m["journal"].(string); ok {
			item.Journal = j
		}
	}
}

// enrichOutputSchema mirrors _enrich_with_output_schema + _build_enrichment_payload
// + _parse_enrichment_response + _extract_paper_enrichment.
func (c *exaC) enrichOutputSchema(ctx context.Context, urls []string, query string) (map[string]map[string]any, error) {
	if len(urls) == 0 || c.apiKey == "" {
		//nolint:nilnil // Django parity: no URLs or no API key means no enrichment
		return nil, nil
	}
	var hosts []string
	for i, u := range urls {
		if i >= 5 {
			break
		}
		if parsed, err := url.Parse(u); err == nil && parsed.Hostname() != "" {
			hosts = append(hosts, parsed.Hostname())
		}
	}
	payload := map[string]any{
		"query":       query + " site:(" + strings.Join(hosts, " OR ") + ")",
		"type":        "deep-lite",
		"category":    "research paper",
		"num_results": minInt(len(urls), 10),
		"contents":    map[string]any{"text": map[string]any{"maxCharacters": 5000}},
		"outputSchema": map[string]any{
			"type":     "object",
			"required": []string{"papers"},
			"properties": map[string]any{
				"papers": map[string]any{
					"type": "array",
					"items": map[string]any{
						"type":     "object",
						"required": []string{"title", "url"},
						"properties": map[string]any{
							"title":            map[string]any{"type": "string"},
							"url":              map[string]any{"type": "string"},
							"authors":          map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
							"year":             map[string]any{"type": "integer"},
							"doi":              map[string]any{"type": "string"},
							"journal":          map[string]any{"type": "string"},
							"is_peer_reviewed": map[string]any{"type": "boolean"},
						},
					},
				},
			},
		},
	}
	headers := map[string]string{
		"Accept":       "application/json",
		"Content-Type": "application/json",
		"x-api-key":    c.apiKey,
	}
	var res struct {
		Output struct {
			Content map[string]any `json:"content"`
		} `json:"output"`
	}
	if err := c.transport.PostJSON(ctx, c.key, c.transport.apiURL(c.key), headers, payload, &res); err != nil {
		return nil, err
	}
	rawPapers, ok := res.Output.Content["papers"].([]any)
	if !ok {
		//nolint:nilnil // Django parity: a response without papers means no enrichment
		return nil, nil
	}
	result := map[string]map[string]any{}
	for _, p := range rawPapers {
		paper, ok := p.(map[string]any)
		if !ok {
			continue
		}
		paperURL := strings.TrimSpace(fmt.Sprint(paper["url"]))
		if paperURL == "" {
			continue
		}
		enrichment := map[string]any{}
		var authors []string
		if alist, ok := paper["authors"].([]any); ok {
			for _, a := range alist {
				if s, ok := a.(string); ok && strings.TrimSpace(s) != "" {
					authors = append(authors, strings.TrimSpace(s))
				}
			}
		}
		if len(authors) > 0 {
			enrichment["authors"] = authors
		}
		if yf, ok := paper["year"].(float64); ok {
			y := int(yf)
			if y >= 1800 && y <= currentMaxPublicationYear() {
				enrichment["year"] = y
			}
		}
		doi := strings.TrimSpace(fmt.Sprint(paper["doi"]))
		if strings.HasPrefix(doi, "10.") {
			enrichment["doi"] = doi
		}
		journal := strings.TrimRight(strings.TrimSpace(fmt.Sprint(paper["journal"])), ".,;:")
		if validateExaJournal(journal) {
			enrichment["journal"] = journal
		}
		if len(enrichment) > 0 {
			result[paperURL] = enrichment
		}
	}
	return result, nil
}
