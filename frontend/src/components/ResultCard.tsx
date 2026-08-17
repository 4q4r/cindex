import { memo, useState, useMemo } from "react";
import {
  Copy,
  ExternalLink,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  XCircle,
  Shield,
  Database,
  Hash,
  FileText,
  Clock,
  OctagonAlert,
  BookOpen,
} from "lucide-react";
import type { CitationStyle, Quote, SearchResult, ViewMode } from "../types";
import { buildCitation } from "../utils/citation";

interface ResultCardProps {
  result: SearchResult;
  query: string;
  viewMode: ViewMode;
  citationStyle: CitationStyle;
  onCopy: (text: string, type: string) => void;
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const parts = useMemo(() => {
    if (!query.trim()) return [text];
    const words = query.split(/\s+/).filter((w) => w.length > 1);
    if (words.length === 0) return [text];

    const regex = new RegExp(
      `(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`,
      "gi",
    );
    return text.split(regex);
  }, [text, query]);

  return (
    <>
      {parts.map((part, i) => {
        const isMatch = query.trim() && parts.length > 1 && i % 2 === 1;
        return isMatch ? (
          <span key={i} className="highlight-match">
            {part}
          </span>
        ) : (
          <span key={i}>{part}</span>
        );
      })}
    </>
  );
}

function CriterionBadge({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: boolean;
  icon: React.ElementType;
}) {
  return (
    <div
      className={`flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${
        value
          ? "bg-success-muted/40 text-success"
          : "bg-bg-elevated text-text-tertiary"
      }`}
    >
      <Icon className="w-3 h-3" />
      <span>{label}</span>
      {value ? (
        <CheckCircle2 className="w-3 h-3" />
      ) : (
        <XCircle className="w-3 h-3 opacity-50" />
      )}
    </div>
  );
}

function ConfidenceBadge({ score }: { score: number }) {
  let level: "high" | "medium" | "low";
  let color: string;
  if (score >= 75) {
    level = "high";
    color = "bg-success-muted/40 text-success";
  } else if (score >= 50) {
    level = "medium";
    color = "bg-warning-muted/40 text-warning";
  } else {
    level = "low";
    color = "bg-danger-muted/40 text-danger";
  }

  const label =
    level === "high"
      ? "Высокая уверенность"
      : level === "medium"
        ? "Средняя уверенность"
        : "Низкая уверенность";

  return (
    <div
      className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${color}`}
    >
      <span>{label}</span>
    </div>
  );
}

function QuotesBlock({
  quotes,
  query,
  preview,
  onCopy,
  isCompact,
  isRetracted,
}: {
  quotes: Quote[];
  query: string;
  preview: string;
  onCopy: (text: string, type: string) => void;
  isCompact: boolean;
  isRetracted: boolean;
}) {
  if (quotes.length === 0) {
    // LLM not configured / no text / extraction failed — fall back to the
    // real abstract preview (a graceful degraded path, never fake).
    return (
      <div
        className={`text-sm ${
          isRetracted ? "text-text-tertiary" : "text-text-secondary"
        } leading-relaxed ${isCompact ? "line-clamp-5" : ""} mb-4`}
      >
        <HighlightedText text={preview} query={query} />
      </div>
    );
  }

  return (
    <section
      aria-label="Извлечённые цитаты"
      className="mb-4 space-y-3 animate-fade-in"
    >
      {quotes.map((q, i) => {
        const relevancePct =
          typeof q.relevance === "number"
            ? Math.round(q.relevance * 100)
            : null;
        return (
          <blockquote
            key={i}
            className={`border-l-2 border-accent/60 pl-3 font-serif ${
              isRetracted ? "text-text-tertiary" : "text-text-secondary"
            } leading-relaxed`}
          >
            <p className={isCompact ? "line-clamp-3" : ""}>
              <HighlightedText text={q.text} query={query} />
            </p>
            <div className="flex items-center justify-between gap-2 mt-1.5 not-italic font-sans">
              <span className="text-text-tertiary text-xs">
                {relevancePct !== null
                  ? `релевантность: ${String(relevancePct)}%`
                  : ""}
                {q.location ? ` · ${q.location}` : ""}
              </span>
              <button
                type="button"
                aria-label="Скопировать цитату"
                onClick={() => {
                  onCopy(q.text, "quote");
                }}
                className="press-feedback flex items-center gap-1 text-text-tertiary hover:text-accent-text bg-bg-elevated border border-border-default px-3 py-1.5 rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
              >
                <Copy className="w-3.5 h-3.5" />
                <span className="text-xs">Копировать</span>
              </button>
            </div>
            {q.rationale && (
              <p className="text-text-tertiary text-xs italic mt-1 font-sans">
                {q.rationale}
              </p>
            )}
          </blockquote>
        );
      })}
    </section>
  );
}

interface TierConfig {
  label: string;
  srText: string;
  className: string;
}

const TIER_CONFIG: Record<
  "A" | "B" | "source-default" | "keyword" | "none",
  TierConfig | null
> = {
  A: {
    label: "Тир A",
    srText: "прямое подтверждение рецензируемости от источника",
    className: "bg-success-muted/40 text-success",
  },
  B: {
    label: "Тир B",
    srText: "вывод по репутации издания",
    className: "bg-accent-subtle/40 text-accent-text",
  },
  "source-default": {
    label: "По умолчанию",
    srText: "предположение по репутации источника",
    className: "bg-warning-muted/40 text-warning",
  },
  keyword: {
    label: "По ключевым словам",
    srText: "оценка по тексту",
    className: "bg-bg-elevated text-text-tertiary",
  },
  none: null,
};

export const ResultCard = memo(function ResultCard({
  result,
  query,
  viewMode,
  citationStyle,
  onCopy,
}: ResultCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [showCitation, setShowCitation] = useState(false);

  const isCompact = viewMode === "compact";
  const isRetracted = result.isRetracted;
  const retractionTitle = result.retractionNote
    ? `Отозвана: ${result.retractionNote}`
    : "Отозвана";
  const tierConfig = TIER_CONFIG[result.tier];
  const citation = useMemo(
    () => buildCitation(result, citationStyle),
    [citationStyle, result],
  );
  const identifiers = useMemo(
    () => Object.entries(result.identifiers),
    [result.identifiers],
  );
  const doi = result.identifiers.doi || result.identifiers.DOI;

  return (
    <article
      className={`bg-bg-card border rounded-xl transition-colors animate-fade-in ${
        isRetracted
          ? "border-danger/60 hover:border-danger/70"
          : "border-border-default hover:border-border-default/80"
      } ${isCompact ? "p-4" : "p-6"}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h3 className="text-[15px] font-medium text-text-primary leading-snug font-serif">
            <HighlightedText text={result.title} query={query} />
          </h3>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1.5 shrink-0">
          {isRetracted && (
            <div
              title={retractionTitle}
              className="flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-danger-muted/60 text-danger"
            >
              <OctagonAlert className="w-3 h-3" aria-hidden="true" />
              <span>Отозвана</span>
              {result.retractionNote && (
                <span className="sr-only">{result.retractionNote}</span>
              )}
            </div>
          )}
          {!isRetracted && (
            <ConfidenceBadge score={result.eligibilityConfidence.overall} />
          )}
        </div>
      </div>

      <div
        className="mt-3 mb-3 border-t border-border-subtle/70"
        aria-hidden="true"
      />

      <div
        className={`text-sm text-text-secondary ${isCompact ? "mb-2" : "mb-3"} leading-relaxed`}
      >
        {result.year ? (
          <span className="text-text-primary font-medium">{result.year}</span>
        ) : (
          <span>Год не указан</span>
        )}
        <span className="text-text-tertiary"> · </span>
        <span
          className={isRetracted ? "text-text-tertiary" : "text-accent-text"}
        >
          {result.source}
        </span>
        {tierConfig && (
          <span
            className={`ml-1.5 inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${tierConfig.className}`}
          >
            <span>{tierConfig.label}</span>
            <span className="sr-only">{tierConfig.srText}</span>
          </span>
        )}
      </div>

      {identifiers.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-3">
          {identifiers.map(([label, value]) => (
            <span
              key={label}
              className="text-xs text-text-tertiary bg-bg-elevated px-2 py-0.5 rounded"
            >
              {label}:{" "}
              <span className="text-text-secondary font-mono">{value}</span>
            </span>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-1.5 mb-4">
        <CriterionBadge
          label="DOI и карточка"
          value={result.eligibilityEvidence.doiAndJournalCard}
          icon={Hash}
        />
        <CriterionBadge
          label="Peer-reviewed"
          value={result.eligibilityEvidence.peerReviewed}
          icon={Shield}
        />
        <CriterionBadge
          label="Индексация"
          value={result.eligibilityEvidence.indexed}
          icon={Database}
        />
        <CriterionBadge
          label="Не preprint"
          value={result.eligibilityEvidence.notPreprint}
          icon={FileText}
        />
      </div>

      <QuotesBlock
        quotes={result.quotes}
        query={query}
        preview={result.preview}
        onCopy={onCopy}
        isCompact={isCompact}
        isRetracted={isRetracted}
      />

      <div className="mb-3">
        <button
          type="button"
          onClick={() => {
            setShowCitation(!showCitation);
          }}
          className="text-xs uppercase tracking-wider text-text-tertiary hover:text-text-secondary flex items-center gap-1 transition-colors"
        >
          {showCitation ? (
            <ChevronUp className="w-3 h-3" />
          ) : (
            <ChevronDown className="w-3 h-3" />
          )}
          Полное цитирование
        </button>
        {showCitation && (
          <div className="mt-2 bg-bg-elevated border border-border-subtle rounded-lg p-3 text-xs text-text-secondary leading-relaxed font-mono">
            {citation}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          onClick={() => {
            onCopy(citation, "citation");
          }}
          className="press-feedback flex items-center gap-1.5 text-xs text-text-tertiary hover:text-accent-text bg-bg-elevated border border-border-default px-3 py-1.5 rounded-lg transition-colors"
        >
          <Copy className="w-3.5 h-3.5" />
          Копировать цитирование
        </button>
        <button
          type="button"
          onClick={() => {
            const quotesText = result.quotes.map((q) => q.text).join("\n\n");
            onCopy(quotesText || result.preview, "quotes");
          }}
          className="press-feedback flex items-center gap-1.5 text-xs text-text-tertiary hover:text-accent-text bg-bg-elevated border border-border-default px-3 py-1.5 rounded-lg transition-colors"
        >
          <Copy className="w-3.5 h-3.5" />
          Копировать цитаты
        </button>
        <a
          href={doi ? `https://doi.org/${doi}` : result.url}
          target="_blank"
          rel="noopener noreferrer"
          className="press-feedback flex items-center gap-1.5 text-xs text-text-tertiary hover:text-accent-text bg-bg-elevated border border-border-default px-3 py-1.5 rounded-lg transition-colors"
        >
          <ExternalLink className="w-3.5 h-3.5" />
          Открыть источник
        </a>
      </div>

      <div className="flex items-center gap-4 mt-3 text-xs text-text-tertiary">
        <div className="flex items-center gap-1">
          <Clock className="w-3 h-3" />
          <span>
            Проверка критериев: {result.eligibilityConfidence.overall}%
          </span>
        </div>
        {doi && (
          <a
            href={`https://openalex.org/works?filter=cites:${encodeURIComponent(doi)}`}
            target="_blank"
            rel="noopener noreferrer"
            title="Открыть список цитирующих работ в OpenAlex"
            className="ml-auto flex items-center gap-1 text-text-tertiary hover:text-accent-text underline underline-offset-2 transition-colors"
          >
            <BookOpen className="w-3 h-3" aria-hidden="true" />
            <span className="tabular-nums whitespace-nowrap">
              Цитирований: {result.citedByCount.toLocaleString("ru-RU")}
            </span>
          </a>
        )}
      </div>

      <button
        type="button"
        onClick={() => {
          setExpanded(!expanded);
        }}
        className="mt-3 text-xs uppercase tracking-wider text-text-tertiary hover:text-text-secondary flex items-center gap-1 transition-colors"
      >
        {expanded ? (
          <ChevronUp className="w-3 h-3" />
        ) : (
          <ChevronDown className="w-3 h-3" />
        )}
        {expanded ? "Скрыть" : "Показать"} детали критериев
      </button>
      {expanded && (
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs text-text-secondary">
          <div>Peer-reviewed: {result.eligibilityConfidence.peerReviewed}%</div>
          <div>Индексация: {result.eligibilityConfidence.indexed}%</div>
          <div>
            DOI/карточка: {result.eligibilityConfidence.doiAndJournalCard}%
          </div>
          <div>Не preprint: {result.eligibilityConfidence.notPreprint}%</div>
        </div>
      )}
    </article>
  );
});
