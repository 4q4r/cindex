export interface ApiSearchResponse {
  query: string;
  count: number;
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
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  error?: string;
  results?: ApiSearchResult[];
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
  rerank_score?: number;
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
  rerankScore?: number;
}

export interface Filters {
  citationStyle: "gost2018" | "mla" | "apa" | "vancouver" | "ieee" | "harvard";
  peerReviewedOnly: boolean;
  indexedOnly: boolean;
  excludePreprints: boolean;
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
  citationStyle: "gost2018",
  peerReviewedOnly: true,
  indexedOnly: true,
  excludePreprints: true,
  dateFrom: "",
  dateTo: "",
  sortBy: "relevance",
};
