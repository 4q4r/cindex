package connector

import (
	"regexp"
	"strings"
)

// CyberLeninka article-body abstract classifier, parity with the module-level
// helpers in html_connectors.py (constants + _cyberleninka_normalize,
// _find_cyberleninka_title, _title_token_overlap, _cyberleninka_issue_is_terminal,
// _classify_cyberleninka_paragraph).

const (
	cyberLeninkaAbstractMax       = 1500
	cyberLeninkaHeaderMax         = 40
	cyberLeninkaAffiliationMax    = 200
	cyberLeninkaTitleFuzz         = 80
	cyberLeninkaTitleTokenMin     = 0.6
	cyberLeninkaTitleOverlapSlack = 20
)

var cyberLeninkaCitationRe = regexp.MustCompile(
	`(?i)\b(?:Том\s+\d{1,4}|Т\.\s*\d{1,4}|вып\.?\s*\d{1,4}|issn[\s:]*\d|udk[\s:]*\d|удк[\s:]*\d|doi[\s:]*10\.\d{4,}|10\.\d{4,})`)
var cyberLeninkaIssueRe = regexp.MustCompile(`(?i)№\s*\d+`)
var cyberLeninkaCodeRe = regexp.MustCompile(`(?i)^\s*(?:from\s+\w|import\s+\w|>>>|#|def\s+\w|class\s+\w|\.{3})`)
var cyberLeninkaRefRe = regexp.MustCompile(`^\s*(?:\d+[\.\)]\s|\[\d+\][\s\.]+)`)
var cyberLeninkaAffiliationRe = regexp.MustCompile(
	`(?i)(?:университет|институт|росси[яй]|научный\s+руководитель|кафедра|студент|аспирант|доцент|профессор|лаборатор)`)
var cyberLeninkaAuthorRe = regexp.MustCompile(`\b[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.`)
var cyberLeninkaVerbStemsRe = regexp.MustCompile(
	`(?i)\b(?:предложен|разработан|рассмотрен|изучен|исследован|показан|применён|применен|описан|представлен|обоснован|проанализирован|получен|найден|определён|определен|установлен|доказан|вычислен|построен|основан|направлен|реализован|апробирован|посвящён|посвящен|проведён|проведен|сделан|выполнен|выявлен|обнаружен|сформулирован|оценён|оценен|выбран|синтезирован|измерён|измерен|рассматривается|исследуется|применяется|описывается|строится)\w*`)

var cyberLeninkaStopHeaderPrefixes = []string{"список ", "библиограф", "references"}
var cyberLeninkaStopHeaderStems = []string{"литература", "источники"}
var cyberLeninkaSkipHeaders = []string{"ключевые слова", "keywords"}
var cyberLeninkaPreviewPrefixes = []string{
	"смотрите также", "читайте также", "см также", "также см", "также читайте",
	"также смотрите", "также в этом номере", "читайте в номере", "смотрите в номере",
	"см в номере", "также в этом выпуске", "читайте в выпуске", "смотрите в выпуске",
	"см в выпуске", "читайте также в выпуске", "в этом номере", "в этом выпуске",
}

var cyberLeninkaBiblioTailChars = "0123456789-/"
var cyberLeninkaIssueSepChars = ".,;\u2014\u2013"
var cyberLeninkaProseAbbrevPrefixes = []string{"табл", "рис", "см", "стр"}

// cyberLeninkaNormalize lowercases, folds OCR variants (й->и, ё->е) and
// collapses non-alphanumeric runs (parity with _cyberleninka_normalize).
func cyberLeninkaNormalize(text string) string {
	folded := strings.NewReplacer("й", "и", "ё", "е").Replace(strings.ToLower(text))
	collapsed := regexp.MustCompile(`[^a-zа-я0-9]+`).ReplaceAllString(folded, " ")
	return strings.TrimSpace(regexp.MustCompile(`\s+`).ReplaceAllString(collapsed, " "))
}

// findCyberLeninkaTitle returns the index of the paragraph matching the
// article title, or -1 (parity with _find_cyberleninka_title).
func findCyberLeninkaTitle(paragraphs []string, normTitle string) int {
	titleTokens := map[string]bool{}
	for _, t := range strings.Fields(normTitle) {
		titleTokens[t] = true
	}
	for idx, para := range paragraphs {
		normPara := cyberLeninkaNormalize(para)
		if normPara == "" {
			continue
		}
		isPreview := false
		for _, prefix := range cyberLeninkaPreviewPrefixes {
			if strings.HasPrefix(normPara, prefix) {
				isPreview = true
				break
			}
		}
		if isPreview {
			continue
		}
		if strings.HasPrefix(normPara, normTitle) {
			return idx
		}
		if len(normPara) > len(normTitle)+cyberLeninkaTitleFuzz {
			continue
		}
		if len(normPara) <= len(normTitle)+cyberLeninkaTitleOverlapSlack &&
			titleTokenOverlap(normPara, titleTokens) {
			return idx
		}
	}
	return -1
}

// titleTokenOverlap reports whether the paragraph shares enough title tokens
// (parity with _title_token_overlap).
func titleTokenOverlap(normPara string, titleTokens map[string]bool) bool {
	if len(titleTokens) == 0 {
		return false
	}
	overlap := 0
	for _, t := range strings.Fields(normPara) {
		if titleTokens[t] {
			overlap++
		}
	}
	return float64(overlap)/float64(len(titleTokens)) >= cyberLeninkaTitleTokenMin
}

// cyberLeninkaIssueIsTerminal mirrors _cyberleninka_issue_is_terminal: a bare
// issue sign ends the abstract run only when it sits at the end of a citation.
func cyberLeninkaIssueIsTerminal(text string) bool {
	matches := cyberLeninkaIssueRe.FindAllStringIndex(text, -1)
	if len(matches) == 0 {
		return false
	}
	match := matches[len(matches)-1]
	before := strings.TrimRight(text[:match[0]], " \t")
	if before != "" {
		stripped := strings.TrimRight(before, ".,;:-\u2014\u2013")
		if stripped != "" {
			fields := strings.Fields(stripped)
			lastToken := strings.ToLower(strings.TrimRight(fields[len(fields)-1], ".,;:-\u2014\u2013"))
			for _, prefix := range cyberLeninkaProseAbbrevPrefixes {
				if lastToken == prefix {
					return false
				}
			}
		}
		lastChar := before[len(before)-1]
		if !strings.ContainsRune(cyberLeninkaIssueSepChars, rune(lastChar)) {
			return false
		}
	}
	tail := regexp.MustCompile(`^[.,;:()\s\x{2014}\x{2013}]+|[.,;:()\s\x{2014}\x{2013}]+$`).
		ReplaceAllString(text[match[1]:], "")
	if tail == "" {
		return true
	}
	tail = regexp.MustCompile(`(?i)^(?:[Сс]\.|стр\.?)\s*`).ReplaceAllString(tail, "")
	tail = regexp.MustCompile(`(?i)\s*г\.?\s*$`).ReplaceAllString(tail, "")
	tail = regexp.MustCompile(`[\s.,;:()]+`).ReplaceAllString(tail, "")
	tail = strings.ReplaceAll(strings.ReplaceAll(tail, "\u2014", "-"), "\u2013", "-")
	if tail == "" {
		return false
	}
	for _, r := range tail {
		if !strings.ContainsRune(cyberLeninkaBiblioTailChars, r) {
			return false
		}
	}
	return true
}

// classifyCyberLeninkaParagraph returns "stop", "skip", or "keep" (parity with
// _classify_cyberleninka_paragraph).
func classifyCyberLeninkaParagraph(text string, started bool) string {
	if strings.TrimSpace(text) == "" {
		return "skip"
	}
	lowered := strings.ToLower(text)
	for _, prefix := range cyberLeninkaStopHeaderPrefixes {
		if strings.HasPrefix(lowered, prefix) {
			return "stop"
		}
	}
	if len(text) <= cyberLeninkaHeaderMax {
		for _, stem := range cyberLeninkaStopHeaderStems {
			if strings.HasPrefix(lowered, stem) {
				return "stop"
			}
		}
	}
	for _, header := range cyberLeninkaSkipHeaders {
		if strings.HasPrefix(lowered, header) {
			return "skip"
		}
	}
	if cyberLeninkaCodeRe.MatchString(text) ||
		cyberLeninkaRefRe.MatchString(text) ||
		cyberLeninkaCitationRe.MatchString(text) {
		return "stop"
	}
	if !started &&
		len(text) < cyberLeninkaAffiliationMax &&
		!cyberLeninkaVerbStemsRe.MatchString(text) &&
		(cyberLeninkaAffiliationRe.MatchString(text) || cyberLeninkaAuthorRe.MatchString(text)) {
		return "skip"
	}
	terminal := cyberLeninkaIssueIsTerminal(text)
	if terminal {
		if started {
			return "stop"
		}
		return "skip"
	}
	return "keep"
}

// extractCyberLeninkaAbstract locates the abstract paragraphs inside the
// article-body block (parity with CyberLeninkaConnector._extract_cyberleninka_abstract).
func extractCyberLeninkaAbstract(paragraphs []string, title string) string {
	normTitle := cyberLeninkaNormalize(title)
	if normTitle == "" {
		return ""
	}
	titleIdx := findCyberLeninkaTitle(paragraphs, normTitle)
	if titleIdx < 0 {
		return ""
	}
	var collected []string
	total := 0
	for _, para := range paragraphs[titleIdx+1:] {
		text := strings.TrimSpace(para)
		action := classifyCyberLeninkaParagraph(text, len(collected) > 0)
		if action == "stop" {
			break
		}
		if action == "skip" {
			continue
		}
		collected = append(collected, text)
		total += len(text)
		if total >= cyberLeninkaAbstractMax {
			break
		}
	}
	return NormalizeScholarly(strings.Join(collected, " "), 2000)
}
