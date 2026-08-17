export interface ApiSearchResponse {
  query: string;
  count: number;
  page?: number;
  per_page?: number;
  total_pages?: number;
  source_stats?: {
    total: number;
    live: number;
    failed: string[];
  };
  results: ApiSearchResult[];
}

export interface ApiSearchJobResponse {
  id: string;
  query: string;
  expression: string;
  status: "queued" | "running" | "completed" | "partial" | "failed";
  stage:
    | "queued"
    | "checking_index"
    | "live_scan"
    | "searching_index"
    | "completed"
    | "failed";
  substage?: string;
  substage_label?: string;
  message: string;
  progress_percent: number;
  source_total: number;
  source_done: number;
  source_live: number;
  source_failed?: string[];
  average_wait_without_enrichment_seconds?: number | null;
  average_wait_with_enrichment_seconds?: number | null;
  source_stats?: {
    total: number;
    live: number;
    failed: string[];
  };
  count?: number;
  index_hits_before?: number;
  index_hits_after?: number;
  rescan_triggered?: boolean;
  rescan_reason?: string;
  freshness_days_used?: number;
  peer_reviewed_only?: boolean;
  indexed_only?: boolean;
  exclude_preprints?: boolean;
  year_from?: number | null;
  year_to?: number | null;
  sort_by?: string;
  page?: number | null;
  per_page?: number | null;
  total_pages?: number | null;
  total_results?: number | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  error?: string;
  results?: ApiSearchResult[];
}

/**
 * Server-side search filters/sort/pagination params sent on job creation.
 * `citationStyle` is frontend-only and intentionally absent here.
 */
export interface SearchJobParams {
  peer_reviewed_only?: boolean;
  indexed_only?: boolean;
  exclude_preprints?: boolean;
  exclude_retracted?: boolean;
  year_from?: number | null;
  year_to?: number | null;
  sort_by?: "relevance" | "newest" | "metadata";
  page?: number;
  per_page?: number;
}

/**
 * Citation style identifier shared across the filter dropdown, the citation
 * builder, and ResultCard. The default is the newest Russian GOST for online
 * articles (ГОСТ Р 7.0.108-2022).
 */
export type CitationStyle =
  | "gost_7_0_108_2022"
  | "gost_7_0_5_2008"
  | "gost2018"
  | "apa"
  | "ieee"
  | "mla"
  | "chicago"
  | "vancouver"
  | "gb_t_7714"
  | "harvard"
  | "bibtex"
  | "ris";

/**
 * A verbatim passage extracted from an article by the PERELMAN LLM pipeline.
 * Cached per-article in the backend (`ArticleQuotes`); surfaced on every
 * search result. `text` is always a verbatim quote from the article.
 */
export interface Quote {
  text: string;
  location?: string;
  relevance?: number;
  rationale?: string;
}

export interface ApiSearchResult {
  id: number;
  title: string;
  preview: string;
  year: number | null;
  publication_date: string | null;
  source: string;
  journal: string;
  authors?: string[];
  volume?: string;
  issue?: string;
  pages?: string;
  doi?: string;
  identifiers?: Record<string, string>;
  eligibility_evidence: {
    peer_reviewed: boolean;
    indexed: boolean;
    doi_and_journal_card: boolean;
    not_preprint: boolean;
  };
  eligibility_confidence: {
    peer_reviewed: number;
    indexed: number;
    doi_and_journal_card: number;
    not_preprint: number;
    overall: number;
  };
  url: string;
  is_retracted: boolean;
  retraction_note: string;
  cited_by_count: number;
  tier: "A" | "B" | "source-default" | "keyword" | "none";
  rerank_score?: number;
  quotes?: Quote[];
  tldr?: string;
}

export interface SearchResult {
  id: string;
  title: string;
  preview: string;
  year: number | null;
  publicationDate: string | null;
  source: string;
  journal: string;
  authors: string[];
  volume: string;
  issue: string;
  pages: string;
  doi: string;
  identifiers: Record<string, string>;
  eligibilityEvidence: {
    peerReviewed: boolean;
    indexed: boolean;
    doiAndJournalCard: boolean;
    notPreprint: boolean;
  };
  eligibilityConfidence: {
    peerReviewed: number;
    indexed: number;
    doiAndJournalCard: number;
    notPreprint: number;
    overall: number;
  };
  url: string;
  isRetracted: boolean;
  retractionNote: string;
  citedByCount: number;
  tier: "A" | "B" | "source-default" | "keyword" | "none";
  rerankScore?: number;
  quotes: Quote[];
  tldr: string;
}

export interface Filters {
  citationStyle: CitationStyle;
  peerReviewedOnly: boolean;
  indexedOnly: boolean;
  excludePreprints: boolean;
  excludeRetracted: boolean;
  dateFrom: string;
  dateTo: string;
  sortBy: "relevance" | "newest" | "metadata";
}

export type SearchState =
  | "idle"
  | "loading"
  | "results"
  | "empty"
  | "error"
  | "partial";
export type ViewMode = "compact" | "comfortable";

export interface SearchProgress {
  jobId: string;
  status: "queued" | "running" | "completed" | "partial" | "failed";
  stage:
    | "queued"
    | "checking_index"
    | "live_scan"
    | "searching_index"
    | "completed"
    | "failed";
  substage: string;
  substageLabel: string;
  message: string;
  percent: number;
  sourceTotal: number;
  sourceDone: number;
  sourceLive: number;
  sourceFailed: string[];
  averageWaitWithoutEnrichmentSeconds: number | null;
  averageWaitWithEnrichmentSeconds: number | null;
  indexHitsBefore: number;
  indexHitsAfter: number;
  rescanTriggered: boolean;
  rescanReason: string;
  freshnessDaysUsed: number;
}

export const DEFAULT_FILTERS: Filters = {
  citationStyle: "gost_7_0_108_2022",
  peerReviewedOnly: true,
  indexedOnly: false,
  excludePreprints: true,
  excludeRetracted: true,
  dateFrom: "",
  dateTo: "",
  sortBy: "relevance",
};
