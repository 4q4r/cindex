import type { CitationStyle, SearchResult } from "../types";

/**
 * Convert a single full name to the GOST initials form ``Surname I. O.`` —
 * the last whitespace token is the surname, preceding tokens become initials
 * (first letter + dot). GOST writes no comma between surname and initials.
 * Works for both Latin and Cyrillic names (operates on the first code point).
 */
function formatAuthorGost(fullName: string): string {
  const parts = fullName.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "";
  if (parts.length === 1) return parts[0];
  const surname = parts[parts.length - 1];
  const given = parts.slice(0, -1);
  const initials = given
    .map((g) => {
      const ch = Array.from(g)[0];
      return ch ? `${ch}.` : "";
    })
    .filter(Boolean)
    .join(" ");
  return `${surname} ${initials}`.trim();
}

/**
 * GOST author list: up to four authors — all listed; five or more — the first
 * three followed by ``[и др.]`` (Russian for "et al."). Each name is rewritten
 * to the ``Surname I. O.`` form and joined with ``, ``.
 */
function formatAuthorsGost(authors: string[]): string {
  const formatted = authors.map((a) => formatAuthorGost(a)).filter(Boolean);
  if (formatted.length === 0) return "Unknown author";
  if (formatted.length <= 4) return formatted.join(", ");
  return `${formatted.slice(0, 3).join(", ")} [и др.]`;
}

/**
 * Western-style author list: up to ``max`` authors — all listed; more — the
 * first ``max`` followed by ``et al.`` Names are kept verbatim (full form).
 */
function formatAuthors(authors: string[], max = 6): string {
  if (!authors.length) return "Unknown author";
  if (authors.length <= max) return authors.join(", ");
  return `${authors.slice(0, max).join(", ")}, et al.`;
}

/** BibTeX-safe identifier for an ``@article`` entry, derived from the DOI. */
function bibtexKey(item: SearchResult): string {
  const year = yearOf(item);
  const firstWord = (item.title || "untitled")
    .trim()
    .split(/\s+/)[0]
    ?.toLowerCase()
    .replace(/[^a-z0-9]/g, "");
  return `${firstWord || "untitled"}${year}`;
}

function yearOf(item: SearchResult): string {
  if (item.year) return String(item.year);
  if (item.publicationDate) return item.publicationDate.slice(0, 4);
  return "n.d.";
}

function journalPart(item: SearchResult): string {
  const volIssue = [item.volume, item.issue ? `(${item.issue})` : ""]
    .filter(Boolean)
    .join("");
  const pages = item.pages ? `:${item.pages}` : "";
  if (!volIssue && !pages) return "";
  return ` ${volIssue}${pages}`;
}

function doiPart(item: SearchResult): string {
  if (item.doi) return ` DOI: ${item.doi}`;
  const fallback = item.identifiers.doi || item.identifiers.DOI;
  return fallback ? ` DOI: ${fallback}` : "";
}

function doiUrl(item: SearchResult): string | null {
  const doi = item.doi || item.identifiers.doi || item.identifiers.DOI;
  return doi ? `https://doi.org/${doi}` : null;
}

/** Access date in DD.MM.YYYY (Russian GOST for electronic references). */
function accessDate(): string {
  const now = new Date();
  const dd = String(now.getDate()).padStart(2, "0");
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const yyyy = String(now.getFullYear());
  return `${dd}.${mm}.${yyyy}`;
}

export function buildCitation(
  item: SearchResult,
  style: CitationStyle,
): string {
  const journal = item.journal || item.source;
  const doi = doiPart(item);
  const url = item.url || (doiUrl(item) ?? "");
  const vol = item.volume || "";
  const issue = item.issue || "";
  const pages = item.pages || "";

  switch (style) {
    case "gost_7_0_108_2022": {
      // ГОСТ Р 7.0.108-2022 — electronic reference. Areas separated by
      // `. —`; surname + space + initials (no comma); DOI optional; access
      // date DD.MM.YYYY.
      const authors = formatAuthorsGost(item.authors);
      const volIssue = [vol, issue ? `№ ${issue}` : ""]
        .filter(Boolean)
        .join(", ");
      const loc = [volIssue, pages ? `С. ${pages}` : ""]
        .filter(Boolean)
        .join(". ");
      const urlPart = url ? ` — URL: ${url}` : "";
      const access = url ? ` (дата обращения: ${accessDate()})` : "";
      const doiPart108 = doi ? ` —${doi}` : "";
      return `${authors}. ${item.title} [Электронный ресурс]. — ${journal}. — ${yearOf(item)}. — ${loc}.${urlPart}${access}.${doiPart108}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "gost_7_0_5_2008": {
      // ГОСТ Р 7.0.5-2008 — classic bibliographic reference.
      const authors = formatAuthorsGost(item.authors);
      const volIssue = [vol, issue ? `№ ${issue}` : ""]
        .filter(Boolean)
        .join(", ");
      const loc = [volIssue, pages ? `С. ${pages}` : ""]
        .filter(Boolean)
        .join(". ");
      return `${authors}. ${item.title} // ${journal}. ${yearOf(item)}. ${loc}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "gost2018": {
      // ГОСТ Р 7.0.100-2018 (kept for backward compatibility).
      const authors = formatAuthorsGost(item.authors);
      return `${authors}. ${item.title}. ${journal}. ${yearOf(item)};${vol || "-"}(${issue || "-"}):${pages || "-"}${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "apa": {
      // APA 7.
      const authors = formatAuthors(item.authors);
      return `${authors} (${yearOf(item)}). ${item.title}. ${journal},${journalPart(item)}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "mla": {
      // MLA 9.
      const authors = formatAuthors(item.authors);
      return `${authors}. "${item.title}." ${journal}, vol. ${vol || "-"}, no. ${issue || "-"}, ${yearOf(item)}, pp. ${pages || "-"}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "chicago": {
      // Chicago 17 (notes-bibliography, journal article).
      const authors = formatAuthors(item.authors);
      return `${authors}. "${item.title}." ${journal} ${vol || "-"}, no. ${issue || "-"} (${yearOf(item)}): ${pages || "-"}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "vancouver": {
      const authors = formatAuthors(item.authors);
      return `${authors}. ${item.title}. ${journal}. ${yearOf(item)};${vol || "-"}(${issue || "-"}):${pages || "-"}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "ieee": {
      const authors = formatAuthors(item.authors);
      return `${authors}, "${item.title}," ${journal}, vol. ${vol || "-"}, no. ${issue || "-"}, pp. ${pages || "-"}, ${yearOf(item)}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "harvard": {
      const authors = formatAuthors(item.authors);
      return `${authors} (${yearOf(item)}) '${item.title}', ${journal}, ${vol || "-"}(${issue || "-"}), pp. ${pages || "-"}${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "gb_t_7714": {
      // GB/T 7714-2015 (Chinese national standard).
      const authors = formatAuthors(item.authors);
      return `${authors}. ${item.title}[J]. ${journal}, ${yearOf(item)}, ${vol || "-"}(${issue || "-"}): ${pages || "-"}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    }
    case "bibtex": {
      // BibTeX @article record.
      const key = bibtexKey(item);
      const author = item.authors.length
        ? item.authors.join(" and ")
        : "Unknown author";
      const fields = [
        `  author = {${author}}`,
        `  title = {${item.title}}`,
        `  journal = {${journal}}`,
        `  year = {${yearOf(item)}}`,
        vol ? `  volume = {${vol}}` : "",
        issue ? `  number = {${issue}}` : "",
        pages ? `  pages = {${pages}}` : "",
        item.doi ? `  doi = {${item.doi}}` : "",
      ].filter(Boolean);
      return `@article{${key},\n${fields.join(",\n")}\n}`;
    }
    case "ris": {
      // RIS record.
      const lines = [
        "TY  - JOUR",
        ...item.authors.map((a) => `AU  - ${a}`),
        `TI  - ${item.title}`,
        `JO  - ${journal}`,
        `PY  - ${yearOf(item)}`,
        vol ? `VL  - ${vol}` : "",
        issue ? `IS  - ${issue}` : "",
        pages ? `SP  - ${pages}` : "",
        item.doi ? `DO  - ${item.doi}` : "",
        url ? `UR  - ${url}` : "",
        "ER  -",
      ].filter(Boolean);
      return lines.join("\n");
    }
  }
}
