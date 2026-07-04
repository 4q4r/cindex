import { useState, useCallback, useMemo, useEffect, useRef } from "react";
import { ChevronLeft, ChevronRight, Check, Filter } from "lucide-react";
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
  SearchProgress,
  SearchResult,
  SearchState,
  ViewMode,
  DEFAULT_FILTERS,
} from "./types";

const PER_PAGE = 5;

export default function App() {
  const [searchState, setSearchState] = useState<SearchState>("idle");
  const [query, setQuery] = useState("");
  const [lastQuery, setLastQuery] = useState("");
  const [filters, setFilters] = useState<Filters>({ ...DEFAULT_FILTERS });
  const [rawResults, setRawResults] = useState<SearchResult[]>([]);
  const [totalBeforeFilter, setTotalBeforeFilter] = useState(0);
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

  const filteredResults = useMemo(() => {
    let results = [...rawResults];

    if (filters.peerReviewedOnly)
      results = results.filter((r) => r.eligibilityEvidence.peerReviewed);
    if (filters.indexedOnly)
      results = results.filter((r) => r.eligibilityEvidence.indexed);
    if (filters.excludePreprints)
      results = results.filter((r) => r.eligibilityEvidence.notPreprint);

    if (filters.dateFrom) {
      const from = parseInt(filters.dateFrom, 10);
      if (!Number.isNaN(from))
        results = results.filter((r) => (r.year ?? 0) >= from);
    }
    if (filters.dateTo) {
      const to = parseInt(filters.dateTo, 10);
      if (!Number.isNaN(to))
        results = results.filter((r) => (r.year ?? 3000) <= to);
    }

    switch (filters.sortBy) {
      case "newest":
        results.sort((a, b) => (b.year ?? 0) - (a.year ?? 0));
        break;
      case "metadata":
        results.sort((a, b) => {
          const scoreA =
            Object.keys(a.identifiers).length +
            Math.round(a.eligibilityConfidence.overall / 25);
          const scoreB =
            Object.keys(b.identifiers).length +
            Math.round(b.eligibilityConfidence.overall / 25);
          return scoreB - scoreA;
        });
        break;
      case "relevance":
      default:
        results.sort(
          (a, b) =>
            b.eligibilityConfidence.overall - a.eligibilityConfidence.overall,
        );
        break;
    }

    return results;
  }, [rawResults, filters]);

  const filteredOutCount = totalBeforeFilter - filteredResults.length;

  const totalPages = Math.ceil(filteredResults.length / PER_PAGE);
  const paginatedResults = useMemo(() => {
    const start = (currentPage - 1) * PER_PAGE;
    return filteredResults.slice(start, start + PER_PAGE);
  }, [filteredResults, currentPage]);

  const pageNumbers = useMemo(() => {
    return Array.from({ length: totalPages }, (_, i) => i + 1).filter(
      (page) => {
        if (page === 1 || page === totalPages) return true;
        return Math.abs(page - currentPage) <= 1;
      },
    );
  }, [currentPage, totalPages]);

  useEffect(() => {
    setCurrentPage(1);
  }, [rawResults]);

  useEffect(() => {
    setCurrentPage(1);
  }, [filters]);

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

  const handleSearch = useCallback(
    async (searchQuery?: string) => {
      const q = searchQuery ?? query;
      if (!q.trim()) return;

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

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
          },
          controller.signal,
        );
        if (searchSeqRef.current !== searchSeq) return;
        setProgress(created);

        const POLL_INTERVAL_MS = 1000;
        const POLL_FAILURE_BACKOFF_MS = 5000;
        const POLL_FAILURE_RETRY_LIMIT = 10;
        let consecutivePollFailures = 0;
        while (searchSeqRef.current === searchSeq) {
          let payload: Awaited<ReturnType<typeof getSearchJob>>;
          try {
            payload = await getSearchJob(created.jobId, controller.signal);
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
            setRawResults(payload.results);
            setTotalBeforeFilter(payload.results.length);
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
        setTotalBeforeFilter(0);
        setSearchState("error");
      }
    },
    [query],
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

  const handleClearStrictFilters = useCallback(() => {
    setFilters((prev) => ({
      ...prev,
      peerReviewedOnly: false,
      indexedOnly: false,
      excludePreprints: false,
    }));
  }, []);

  const handleExampleClick = useCallback(
    (q: string) => {
      setQuery(q);
      void handleSearch(q);
    },
    [handleSearch],
  );

  const allFilteredOut = rawResults.length > 0 && filteredResults.length === 0;
  const showResults =
    searchState === "results" ||
    (searchState === "partial" && filteredResults.length > 0);

  return (
    <div className="min-h-screen bg-bg-primary">
      <Header
        resultCount={filteredResults.length}
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

          <div className="flex-1 min-w-0 h-[calc(100vh-92px)] overflow-y-auto pr-1">
            <SearchBar
              query={query}
              onQueryChange={setQuery}
              onSearch={() => {
                void handleSearch();
              }}
              isLoading={searchState === "loading"}
            />

            <div className="py-5">
              {showResults && (
                <div className="mb-5">
                  <InfoBar
                    totalResults={filteredResults.length}
                    filteredOut={filteredOutCount}
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

              {searchState === "partial" && filteredResults.length > 0 && (
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
                  {paginatedResults.map((result, index) => (
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
                          setCurrentPage((p) => Math.max(1, p - 1));
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
                                  setCurrentPage(page);
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
                          setCurrentPage((p) => Math.min(totalPages, p + 1));
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
                (searchState === "partial" &&
                  filteredResults.length === 0 &&
                  !allFilteredOut)) && (
                <EmptyState
                  state={searchState}
                  onRetry={() => {
                    void handleSearch();
                  }}
                  onExampleClick={handleExampleClick}
                  sourcesFailed={sourcesFailed}
                />
              )}

              {allFilteredOut && (
                <div className="flex flex-col items-center justify-center py-12 px-4 animate-fade-in">
                  <div className="max-w-md text-center">
                    <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-bg-elevated flex items-center justify-center">
                      <Filter className="w-7 h-7 text-text-tertiary" />
                    </div>
                    <h2 className="text-lg font-semibold text-text-primary mb-2">
                      Все результаты отфильтрованы
                    </h2>
                    <p className="text-sm text-text-secondary mb-4 leading-relaxed">
                      Найдено {totalBeforeFilter}{" "}
                      {totalBeforeFilter === 1
                        ? "результат"
                        : totalBeforeFilter < 5
                          ? "результата"
                          : "результатов"}
                      , но ни один не прошёл строгие фильтры. Ослабьте фильтры
                      слева, чтобы увидеть результаты.
                    </p>
                    <button
                      type="button"
                      onClick={handleClearStrictFilters}
                      className="inline-flex items-center gap-2 px-5 py-2.5 bg-accent hover:bg-accent-muted text-white text-sm font-medium rounded-lg transition-colors"
                    >
                      Сбросить строгие фильтры
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
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
