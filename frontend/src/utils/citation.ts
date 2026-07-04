import { SearchResult } from "../types";

type CitationStyle =
  | "gost2018"
  | "mla"
  | "apa"
  | "vancouver"
  | "ieee"
  | "harvard";

function formatAuthors(authors: string[], max = 6): string {
  if (!authors.length) return "Unknown author";
  if (authors.length <= max) return authors.join(", ");
  return `${authors.slice(0, max).join(", ")}, et al.`;
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
  const fallback = item.identifiers?.doi || item.identifiers?.DOI;
  return fallback ? ` DOI: ${fallback}` : "";
}

export function buildCitation(
  item: SearchResult,
  style: CitationStyle,
): string {
  const authors = formatAuthors(item.authors);
  const year = yearOf(item);
  const journal = item.journal || item.source;
  const doi = doiPart(item);

  switch (style) {
    case "mla":
      return `${authors}. "${item.title}." ${journal}, ${year},${journalPart(item)}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    case "apa":
      return `${authors} (${year}). ${item.title}. ${journal},${journalPart(item)}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    case "vancouver":
      return `${authors}. ${item.title}. ${journal}. ${year};${item.volume || "-"}(${item.issue || "-"}):${item.pages || "-"}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    case "ieee":
      return `${authors}, "${item.title}," ${journal}, vol. ${item.volume || "-"}, no. ${item.issue || "-"}, pp. ${item.pages || "-"}, ${year}.${doi}`
        .replace(/\s+/g, " ")
        .trim();
    case "harvard":
      return `${authors} (${year}) '${item.title}', ${journal}, ${item.volume || "-"}(${item.issue || "-"}), pp. ${item.pages || "-"}${doi}`
        .replace(/\s+/g, " ")
        .trim();
    case "gost2018":
    default:
      return `${authors}. ${item.title}. ${journal}. ${year};${item.volume || "-"}(${item.issue || "-"}):${item.pages || "-"}${doi}`
        .replace(/\s+/g, " ")
        .trim();
  }
}
