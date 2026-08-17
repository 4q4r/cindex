package domain

import (
	"fmt"
	"strconv"
	"strings"
)

const maxShortAuthors = 3

// RenderCitation renders a bibliographic citation. Parity with
// apps.articles.services.CitationService.render.
func RenderCitation(a *Article, authors []string, style string) string {
	authorsStr := "Unknown author"
	if len(authors) > 0 {
		if len(authors) > maxShortAuthors {
			authorsStr = strings.Join(authors[:maxShortAuthors], ", ") + " [et al.]"
		} else {
			authorsStr = strings.Join(authors, ", ")
		}
	}

	year := "n.d."
	if a.PubYear != nil {
		year = strconv.Itoa(*a.PubYear)
	}
	journal := "Unknown journal"
	if a.Journal != nil && a.Journal.Name != "" {
		journal = a.Journal.Name
	}
	doi := ""
	if a.DOI != "" {
		doi = " DOI: " + a.DOI
	}

	if style == "gost_2003" {
		return strings.TrimSpace(fmt.Sprintf(
			"%s %s // %s. %s. Т. %s № %s. С. %s%s",
			authorsStr, a.Title, journal, year,
			orDash(a.Volume), orDash(a.Issue), orDash(a.Pages), doi,
		))
	}
	return strings.TrimSpace(fmt.Sprintf(
		"%s. %s. %s. %s;%s(%s):%s%s",
		authorsStr, a.Title, journal, year,
		orDash(a.Volume), orDash(a.Issue), orDash(a.Pages), doi,
	))
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}
