package domain

import "testing"

func citationArticle() *Article {
	year := 2024
	return &Article{
		Title:   "Deep Learning in Medicine",
		PubYear: &year,
		Volume:  "12",
		Issue:   "3",
		Pages:   "45-60",
		DOI:     "10.1234/example",
		Journal: &Journal{Name: "Journal of AI"},
	}
}

func TestRenderCitationGost2018(t *testing.T) {
	a := citationArticle()
	got := RenderCitation(a, []string{"Иванов И.И.", "Петров П.П."}, "gost_2018")
	want := "Иванов И.И., Петров П.П.. Deep Learning in Medicine. Journal of AI. 2024;12(3):45-60 DOI: 10.1234/example"
	if got != want {
		t.Errorf("got  %q\nwant %q", got, want)
	}
}

func TestRenderCitationGost2003(t *testing.T) {
	a := citationArticle()
	got := RenderCitation(a, []string{"Иванов И.И."}, "gost_2003")
	want := "Иванов И.И. Deep Learning in Medicine // Journal of AI. 2024. Т. 12 № 3. С. 45-60 DOI: 10.1234/example"
	if got != want {
		t.Errorf("got  %q\nwant %q", got, want)
	}
}

func TestRenderCitationEtAl(t *testing.T) {
	a := citationArticle()
	got := RenderCitation(a, []string{"A", "B", "C", "D"}, "gost_2018")
	want := "A, B, C [et al.]. Deep Learning in Medicine. Journal of AI. 2024;12(3):45-60 DOI: 10.1234/example"
	if got != want {
		t.Errorf("got  %q\nwant %q", got, want)
	}
}

func TestRenderCitationFallbacks(t *testing.T) {
	a := citationArticle()
	a.PubYear = nil
	a.DOI = ""
	a.Journal = nil
	a.Volume, a.Issue, a.Pages = "", "", ""
	got := RenderCitation(a, nil, "gost_2018")
	want := "Unknown author. Deep Learning in Medicine. Unknown journal. n.d.;-(-):-"
	if got != want {
		t.Errorf("got  %q\nwant %q", got, want)
	}
}
