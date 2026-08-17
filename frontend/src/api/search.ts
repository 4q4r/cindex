import {
  ApiSearchJobResponse,
  ApiSearchResponse,
  SearchJobParams,
  SearchProgress,
  SearchResult,
} from "../types";

const API_PREFIX = "/api/v1";

export interface SearchApiRequest extends SearchJobParams {
  query: string;
  expression?: string;
  force_refresh?: boolean;
}

export interface SearchApiPayload {
  results: SearchResult[];
  sourceStats: {
    total: number;
    live: number;
    failed: string[];
  };
  totalResults?: number;
  totalPages?: number;
  page?: number;
  perPage?: number;
}

export async function getSourceStats(
  signal?: AbortSignal,
): Promise<SearchApiPayload["sourceStats"]> {
  const response = await fetch(`${API_PREFIX}/source-stats`, {
    method: "GET",
    signal,
  });
  if (!response.ok) {
    throw new Error(`Source stats API failed: ${String(response.status)}`);
  }
  const data = (await response.json()) as {
    total?: number;
    live?: number;
    failed?: string[];
  };
  return {
    total: data.total ?? 0,
    live: data.live ?? 0,
    failed: data.failed ?? [],
  };
}

function normalizeConfidence(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value <= 1) return Math.round(value * 100);
  if (value > 100) return 100;
  return Math.round(value);
}

export function mapApiResult(
  item: ApiSearchResponse["results"][number],
): SearchResult {
  return {
    id: String(item.id),
    title: item.title,
    preview: item.preview,
    year: item.year,
    publicationDate: item.publication_date,
    source: item.source,
    journal: item.journal,
    authors: item.authors ?? [],
    volume: item.volume ?? "",
    issue: item.issue ?? "",
    pages: item.pages ?? "",
    doi: item.doi ?? "",
    identifiers: item.identifiers ?? {},
    eligibilityEvidence: {
      peerReviewed: item.eligibility_evidence.peer_reviewed,
      indexed: item.eligibility_evidence.indexed,
      doiAndJournalCard: item.eligibility_evidence.doi_and_journal_card,
      notPreprint: item.eligibility_evidence.not_preprint,
    },
    eligibilityConfidence: {
      peerReviewed: normalizeConfidence(
        item.eligibility_confidence.peer_reviewed,
      ),
      indexed: normalizeConfidence(item.eligibility_confidence.indexed),
      doiAndJournalCard: normalizeConfidence(
        item.eligibility_confidence.doi_and_journal_card,
      ),
      notPreprint: normalizeConfidence(
        item.eligibility_confidence.not_preprint,
      ),
      overall: normalizeConfidence(item.eligibility_confidence.overall),
    },
    url: item.url,
    isRetracted: item.is_retracted,
    retractionNote: item.retraction_note,
    citedByCount: item.cited_by_count,
    tier: item.tier,
    rerankScore: item.rerank_score ?? 0,
    quotes: (item.quotes ?? []).map((q) => ({
      text: q.text,
      location: q.location,
      relevance: q.relevance,
      rationale: q.rationale,
    })),
  };
}

export async function searchArticles(
  payload: SearchApiRequest,
): Promise<SearchApiPayload> {
  const response = await fetch(`${API_PREFIX}/search`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Search API failed: ${String(response.status)}`);
  }

  const data = (await response.json()) as ApiSearchResponse;
  return {
    results: data.results.map(mapApiResult),
    sourceStats: {
      total: data.source_stats?.total ?? 0,
      live: data.source_stats?.live ?? 0,
      failed: data.source_stats?.failed ?? [],
    },
    totalResults: data.count,
    totalPages: data.total_pages,
    page: data.page,
    perPage: data.per_page,
  };
}

export async function createSearchJob(
  payload: SearchApiRequest,
  signal?: AbortSignal,
): Promise<SearchProgress> {
  const response = await fetch(`${API_PREFIX}/search/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    throw new Error(`Search job create failed: ${String(response.status)}`);
  }
  const data = (await response.json()) as ApiSearchJobResponse;
  return {
    jobId: data.id,
    status: data.status,
    stage: data.stage,
    substage: data.substage ?? "",
    substageLabel: data.substage_label ?? "",
    message: data.message,
    percent: data.progress_percent,
    sourceTotal: data.source_total,
    sourceDone: data.source_done,
    sourceLive: data.source_live,
    sourceFailed: data.source_failed ?? [],
    averageWaitWithoutEnrichmentSeconds:
      data.average_wait_without_enrichment_seconds ?? null,
    averageWaitWithEnrichmentSeconds:
      data.average_wait_with_enrichment_seconds ?? null,
    indexHitsBefore: data.index_hits_before ?? 0,
    indexHitsAfter: data.index_hits_after ?? 0,
    rescanTriggered: data.rescan_triggered ?? false,
    rescanReason: data.rescan_reason ?? "",
    freshnessDaysUsed: data.freshness_days_used ?? 14,
  };
}

export interface SearchJobResultPayload extends SearchApiPayload {
  progress: SearchProgress;
  status: "queued" | "running" | "completed" | "partial" | "failed";
  error: string;
}

export async function getSearchJob(
  jobId: string,
  page?: number,
  perPage?: number,
  signal?: AbortSignal,
): Promise<SearchJobResultPayload> {
  const params = new URLSearchParams();
  if (page) params.set("page", String(page));
  if (perPage) params.set("per_page", String(perPage));
  const qs = params.toString();
  const url = `${API_PREFIX}/search/jobs/${jobId}${qs ? `?${qs}` : ""}`;
  const response = await fetch(url, {
    method: "GET",
    signal,
  });
  if (!response.ok) {
    throw new Error(`Search job get failed: ${String(response.status)}`);
  }
  const data = (await response.json()) as ApiSearchJobResponse;
  return {
    status: data.status,
    error: data.error ?? "",
    results: (data.results ?? []).map(mapApiResult),
    sourceStats: {
      total: data.source_stats?.total ?? 0,
      live: data.source_stats?.live ?? 0,
      failed: data.source_stats?.failed ?? [],
    },
    totalResults: data.total_results ?? undefined,
    totalPages: data.total_pages ?? undefined,
    page: data.page ?? undefined,
    perPage: data.per_page ?? undefined,
    progress: {
      jobId: data.id,
      status: data.status,
      stage: data.stage,
      substage: data.substage ?? "",
      substageLabel: data.substage_label ?? "",
      message: data.message,
      percent: data.progress_percent,
      sourceTotal: data.source_total,
      sourceDone: data.source_done,
      sourceLive: data.source_live,
      sourceFailed: data.source_failed ?? [],
      averageWaitWithoutEnrichmentSeconds:
        data.average_wait_without_enrichment_seconds ?? null,
      averageWaitWithEnrichmentSeconds:
        data.average_wait_with_enrichment_seconds ?? null,
      indexHitsBefore: data.index_hits_before ?? 0,
      indexHitsAfter: data.index_hits_after ?? 0,
      rescanTriggered: data.rescan_triggered ?? false,
      rescanReason: data.rescan_reason ?? "",
      freshnessDaysUsed: data.freshness_days_used ?? 14,
    },
  };
}
