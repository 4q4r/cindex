import { useState, useCallback, useMemo, useEffect, useRef } from "react";
import { ChevronLeft, ChevronRight, Check } from "lucide-react";
import { Header } from "./components/Header";
import { SearchBar } from "./components/SearchBar";
import { FilterPanel } from "./components/FilterPanel";
import { InfoBar } from "./components/InfoBar";
import { ResultCard } from "./components/ResultCard";
import { EmptyState } from "./components/EmptyState";
import { LoadingState } from "./components/LoadingState";
import { createSearchJob, getSearchJob, getSourceStats } from "./api/search";
import {
  Filters,
  SearchJobParams,
  SearchProgress,
  SearchResult,
  SearchState,
  ViewMode,
  DEFAULT_FILTERS,
} from "./types";

const PER_PAGE = 5;
const FILTER_CHANGE_DEBOUNCE_MS = 400;

function parseYear(value: string): number | null {
  if (!value) return null;
  const parsed = parseInt(value, 10);
  return Number.isNaN(parsed) ? null : parsed;
}

function buildParams(filters: Filters): SearchJobParams {
  return {
    peer_reviewed_only: filters.peerReviewedOnly,
    indexed_only: filters.indexedOnly,
    exclude_preprints: filters.excludePreprints,
    year_from: parseYear(filters.dateFrom),
    year_to: parseYear(filters.dateTo),
    sort_by: filters.sortBy,
  };
}

export default function App() {
  const [searchState, setSearchState] = useState<SearchState>("idle");
  const [query, setQuery] = useState("");
  const [lastQuery, setLastQuery] = useState("");
  const [filters, setFilters] = useState<Filters>({ ...DEFAULT_FILTERS });
  const [rawResults, setRawResults] = useState<SearchResult[]>([]);
  const [totalResults, setTotalResults] = useState(0);
  const [totalPages, setTotalPages] = useState(0);
  const [sourcesQueried, setSourcesQueried] = useState(0);
  const [sourcesFailed, setSourcesFailed] = useState<string[]>([]);
  const [lastSearchTime, setLastSearchTime] = useState<Date | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("comfortable");
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);
  const [currentPage, setCurrentPage] = useState(1);
  const [copyNotification, setCopyNotification] = useState<string | null>(null);
  const [progress, setProgress] = useState<SearchProgress | null>(null);
  const notifTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const pageAbortRef = useRef<AbortController | null>(null);
  const currentJobIdRef = useRef<string | null>(null);
  const filtersRef = useRef(filters);
  const lastQueryRef = useRef("");
  const didMountRef = useRef(false);

  filtersRef.current = filters;

  useEffect(() => {
    const controller = new AbortController();
    getSourceStats(controller.signal)
      .then((stats) => {
        setSourcesQueried(stats.total);
        setSourcesFailed(stats.failed);
      })
      .catch(() => {
        setSourcesQueried(0);
        setSourcesFailed([]);
      });
    return () => {
      controller.abort();
    };
  }, []);

  const hasActiveFilters = useMemo(() => {
    return (
      filters.peerReviewedOnly ||
      filters.indexedOnly ||
      filters.excludePreprints ||
      filters.dateFrom !== "" ||
      filters.dateTo !== "" ||
      filters.sortBy !== "relevance"
    );
  }, [filters]);

  const applyJobPayload = useCallback(
    (payload: Awaited<ReturnType<typeof getSearchJob>>) => {
      setRawResults(payload.results);
      setTotalResults(payload.totalResults ?? payload.results.length);
      setTotalPages(payload.totalPages ?? 0);
      setCurrentPage(payload.page ?? 1);
      setSourcesQueried(payload.sourceStats.total);
      setSourcesFailed(
        payload.progress.sourceFailed.length > 0
          ? payload.progress.sourceFailed
          : payload.sourceStats.failed,
      );
    },
    [],
  );

  const fetchPage = useCallback(
    async (page: number) => {
      const jobId = currentJobIdRef.current;
      if (!jobId) return;
      pageAbortRef.current?.abort();
      const controller = new AbortController();
      pageAbortRef.current = controller;
      try {
        const payload = await getSearchJob(
          jobId,
          page,
          PER_PAGE,
          controller.signal,
        );
        applyJobPayload(payload);
      } catch {
        /* aborted or transient page fetch — keep current page */
      }
    },
    [applyJobPayload],
  );

  const handleSearch = useCallback(
    async (searchQuery?: string) => {
      const q = searchQuery ?? query;
      if (!q.trim()) return;

      abortRef.current?.abort();
      pageAbortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      lastQueryRef.current = q;
      setLastQuery(q);
      setSearchState("loading");
      setCurrentPage(1);
      setProgress(null);
      searchSeqRef.current += 1;
      const searchSeq = searchSeqRef.current;

      try {
        const created = await createSearchJob(
          {
            query: q,
            force_refresh: false,
            ...buildParams(filtersRef.current),
          },
          controller.signal,
        );
        if (searchSeqRef.current !== searchSeq) return;
        setProgress(created);
        currentJobIdRef.current = created.jobId;

        const POLL_INTERVAL_MS = 1000;
        const POLL_FAILURE_BACKOFF_MS = 5000;
        const POLL_FAILURE_RETRY_LIMIT = 10;
        let consecutivePollFailures = 0;
        while (searchSeqRef.current === searchSeq) {
          let payload: Awaited<ReturnType<typeof getSearchJob>>;
          try {
            payload = await getSearchJob(
              created.jobId,
              1,
              PER_PAGE,
              controller.signal,
            );
            consecutivePollFailures = 0;
          } catch {
            if (searchSeqRef.current !== searchSeq) return;
            consecutivePollFailures += 1;
            if (consecutivePollFailures < POLL_FAILURE_RETRY_LIMIT) {
              await new Promise((resolve) =>
                setTimeout(resolve, POLL_INTERVAL_MS),
              );
              continue;
            }
            await new Promise((resolve) =>
              setTimeout(resolve, POLL_FAILURE_BACKOFF_MS),
            );
            continue;
          }
          if (searchSeqRef.current !== searchSeq) return;
          setProgress(payload.progress);
          setSourcesQueried(payload.sourceStats.total);
          setSourcesFailed(
            payload.progress.sourceFailed.length > 0
              ? payload.progress.sourceFailed
              : payload.sourceStats.failed,
          );

          if (payload.status === "failed") {
            throw new Error(payload.error || "Search job failed");
          }
          if (payload.status === "completed" || payload.status === "partial") {
            applyJobPayload(payload);
            setLastSearchTime(new Date());

            const failedSources =
              payload.progress.sourceFailed.length > 0
                ? payload.progress.sourceFailed
                : payload.sourceStats.failed;

            if (payload.results.length === 0) {
              setSearchState("empty");
            } else if (
              failedSources.length > 0 ||
              payload.status === "partial"
            ) {
              setSearchState("partial");
            } else {
              setSearchState("results");
            }
            return;
          }

          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        }
      } catch {
        if (searchSeqRef.current !== searchSeq) return;
        setRawResults([]);
        setTotalResults(0);
        setTotalPages(0);
        setSearchState("error");
      }
    },
    [query, applyJobPayload],
  );

  // Re-run the search server-side when filters/sort change (debounced), since
  // filters are baked into the job at creation time. Skip the initial mount.
  useEffect(() => {
    if (!didMountRef.current) {
      didMountRef.current = true;
      return;
    }
    if (!lastQueryRef.current) return;
    const timer = setTimeout(() => {
      void handleSearch(lastQueryRef.current);
    }, FILTER_CHANGE_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  const handlePageChange = useCallback(
    (page: number) => {
      const next = Math.min(Math.max(1, page), Math.max(1, totalPages));
      if (next === currentPage) return;
      setCurrentPage(next);
      void fetchPage(next);
    },
    [currentPage, totalPages, fetchPage],
  );

  const handleCopy = useCallback(async (text: string, type: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyNotification(
        type === "citation" ? "Цитирование скопировано" : "Превью скопировано",
      );
      if (notifTimeoutRef.current) clearTimeout(notifTimeoutRef.current);
      notifTimeoutRef.current = setTimeout(() => {
        setCopyNotification(null);
      }, 2000);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      document.body.appendChild(textarea);
      textarea.select();
      // eslint-disable-next-line @typescript-eslint/no-deprecated -- legacy clipboard fallback for non-secure contexts where navigator.clipboard is unavailable
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopyNotification(
        type === "citation" ? "Цитирование скопировано" : "Превью скопировано",
      );
      if (notifTimeoutRef.current) clearTimeout(notifTimeoutRef.current);
      notifTimeoutRef.current = setTimeout(() => {
        setCopyNotification(null);
      }, 2000);
    }
  }, []);

  const handleClearFilters = useCallback(() => {
    setFilters({ ...DEFAULT_FILTERS });
  }, []);

  const handleExampleClick = useCallback(
    (q: string) => {
      setQuery(q);
      void handleSearch(q);
    },
    [handleSearch],
  );

  const showResults =
    searchState === "results" ||
    (searchState === "partial" && rawResults.length > 0);

  const pageNumbers = useMemo(() => {
    return Array.from({ length: totalPages }, (_, i) => i + 1).filter(
      (page) => {
        if (page === 1 || page === totalPages) return true;
        return Math.abs(page - currentPage) <= 1;
      },
    );
  }, [currentPage, totalPages]);

  return (
    <div className="min-h-screen bg-bg-primary">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-[100] focus:px-4 focus:py-2.5 focus:rounded-lg focus:bg-bg-card focus:text-text-primary focus:border focus:border-accent focus:shadow-xl"
      >
        Перейти к содержимому
      </a>
      <Header
        resultCount={totalResults}
        lastSearchTime={lastSearchTime}
        sourcesQueried={sourcesQueried}
        sourcesFailed={sourcesFailed}
        searchState={searchState}
      />

      <div className="max-w-[1440px] mx-auto px-6 py-6">
        <div className="flex gap-8">
          <FilterPanel
            filters={filters}
            onFiltersChange={setFilters}
            isMobileOpen={mobileFiltersOpen}
            onMobileClose={() => {
              setMobileFiltersOpen(false);
            }}
          />

          <main
            id="main-content"
            className="flex-1 min-w-0 h-[calc(100vh-92px)] overflow-y-auto pr-1"
          >
            <SearchBar
              query={query}
              onQueryChange={setQuery}
              onSearch={() => {
                void handleSearch();
              }}
              isLoading={searchState === "loading"}
            />

            <div className="py-5">
              <h2 className="sr-only">Результаты поиска</h2>
              {showResults && (
                <div className="mb-5">
                  <InfoBar
                    totalResults={totalResults}
                    filteredOut={0}
                    sourcesFailed={sourcesFailed}
                    onClearFilters={handleClearFilters}
                    onToggleMobileFilters={() => {
                      setMobileFiltersOpen(true);
                    }}
                    viewMode={viewMode}
                    onViewModeChange={setViewMode}
                    hasActiveFilters={hasActiveFilters}
                  />
                </div>
              )}

              {searchState === "partial" && rawResults.length > 0 && (
                <div className="mb-5 flex items-center gap-2 bg-warning-muted/20 border border-warning/20 rounded-lg px-4 py-2.5 text-xs text-warning">
                  <span>
                    Частичный результат: часть источников недоступна (
                    {sourcesFailed.join(", ")}).
                  </span>
                </div>
              )}

              {searchState === "loading" && (
                <LoadingState progress={progress} />
              )}

              {showResults && (
                <div className="space-y-5">
                  {rawResults.map((result, index) => (
                    <div
                      key={result.id}
                      style={{ animationDelay: `${String(index * 60)}ms` }}
                    >
                      <ResultCard
                        result={result}
                        query={lastQuery}
                        viewMode={viewMode}
                        citationStyle={filters.citationStyle}
                        onCopy={(text, type) => {
                          void handleCopy(text, type);
                        }}
                      />
                    </div>
                  ))}

                  {totalPages > 1 && (
                    <div className="flex items-center justify-center gap-2 pt-6 pb-2">
                      <button
                        type="button"
                        onClick={() => {
                          handlePageChange(currentPage - 1);
                        }}
                        disabled={currentPage === 1}
                        className="flex items-center gap-1 px-3 py-2 text-xs text-text-secondary hover:text-text-primary bg-bg-elevated border border-border-default rounded-lg disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >
                        <ChevronLeft className="w-4 h-4" />
                        Назад
                      </button>
                      <div className="flex items-center gap-1">
                        {pageNumbers.map((page, idx, arr) => {
                          const prevPage = arr[idx - 1];
                          const showEllipsis = Boolean(
                            prevPage && page - prevPage > 1,
                          );
                          return (
                            <span key={page} className="flex items-center">
                              {showEllipsis && (
                                <span className="px-2 text-text-tertiary text-xs">
                                  ...
                                </span>
                              )}
                              <button
                                type="button"
                                onClick={() => {
                                  handlePageChange(page);
                                }}
                                className={`w-9 h-9 rounded-lg text-xs font-medium transition-colors ${
                                  page === currentPage
                                    ? "bg-accent text-white"
                                    : "text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                                }`}
                              >
                                {page}
                              </button>
                            </span>
                          );
                        })}
                      </div>
                      <button
                        type="button"
                        onClick={() => {
                          handlePageChange(currentPage + 1);
                        }}
                        disabled={currentPage === totalPages}
                        className="flex items-center gap-1 px-3 py-2 text-xs text-text-secondary hover:text-text-primary bg-bg-elevated border border-border-default rounded-lg disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >
                        Далее
                        <ChevronRight className="w-4 h-4" />
                      </button>
                    </div>
                  )}
                </div>
              )}

              {(searchState === "idle" ||
                searchState === "empty" ||
                searchState === "error" ||
                (searchState === "partial" && rawResults.length === 0)) && (
                <EmptyState
                  state={searchState}
                  onRetry={() => {
                    void handleSearch();
                  }}
                  onExampleClick={handleExampleClick}
                  sourcesFailed={sourcesFailed}
                />
              )}
            </div>
          </main>
        </div>
      </div>

      {copyNotification && (
        <div className="fixed bottom-6 right-6 z-50 flex items-center gap-2 bg-bg-card border border-success/30 text-success px-4 py-2.5 rounded-lg shadow-xl animate-fade-in">
          <Check className="w-4 h-4" />
          <span className="text-sm">{copyNotification}</span>
        </div>
      )}
    </div>
  );
}
