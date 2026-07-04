import { memo } from "react";
import {
  BookOpen,
  Search,
  SearchX,
  AlertCircle,
  RefreshCw,
  ArrowRight,
} from "lucide-react";
import { SearchState } from "../types";

interface EmptyStateProps {
  state: SearchState;
  onRetry: () => void;
  onExampleClick: (query: string) => void;
  sourcesFailed: string[];
}

const exampleQueries = [
  {
    query: "deep learning medical imaging",
    description: "ИИ в медицинской визуализации",
  },
  { query: "CRISPR Huntington", description: "CRISPR при болезни Хантингтона" },
  {
    query: "ocean acidification coral reef",
    description: "Влияние закисления океана на рифы",
  },
  {
    query: "antibiotic resistance agriculture",
    description: "Антибиотикорезистентность в агросекторе",
  },
  {
    query: "машинное обучение прогнозирование",
    description: "Русскоязычные публикации по ML",
  },
];

export const EmptyState = memo(function EmptyState({
  state,
  onRetry,
  onExampleClick,
  sourcesFailed,
}: EmptyStateProps) {
  if (state === "idle") {
    return (
      <div className="flex flex-col items-center justify-center py-6 lg:py-2 px-4 animate-fade-in">
        <div className="max-w-5xl w-full text-center">
          <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-accent-subtle/30 flex items-center justify-center">
            <BookOpen className="w-7 h-7 text-accent" />
          </div>
          <h2 className="text-xl font-semibold text-text-primary mb-2">
            Поиск научных источников
          </h2>
          <p className="text-sm text-text-secondary mb-4 leading-relaxed max-w-2xl mx-auto">
            Ищите релевантные peer-reviewed публикации и сразу получайте краткое
            preview статьи, проверку критериев и готовое цитирование по ГОСТ.
          </p>
          <div className="text-left">
            <h3 className="text-[11px] uppercase tracking-wider text-text-tertiary text-center mb-3">
              Примеры запросов
            </h3>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-2.5">
              {exampleQueries.map((ex) => (
                <button
                  type="button"
                  key={ex.query}
                  onClick={() => {
                    onExampleClick(ex.query);
                  }}
                  className="w-full group flex items-center gap-3 bg-bg-card border border-border-default rounded-lg p-3 hover:border-accent/30 hover:bg-bg-elevated transition-all text-left"
                >
                  <Search className="w-4 h-4 text-text-tertiary group-hover:text-accent transition-colors shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-text-primary font-medium">
                      {ex.query}
                    </div>
                    <div className="text-[11px] text-text-tertiary">
                      {ex.description}
                    </div>
                  </div>
                  <ArrowRight className="w-4 h-4 text-text-tertiary opacity-0 group-hover:opacity-100 group-hover:text-accent transition-all shrink-0" />
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (state === "empty") {
    return (
      <div className="flex flex-col items-center justify-center py-20 px-4 animate-fade-in">
        <div className="max-w-md text-center">
          <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-bg-elevated flex items-center justify-center">
            <SearchX className="w-7 h-7 text-text-tertiary" />
          </div>
          <h2 className="text-lg font-semibold text-text-primary mb-2">
            Ничего не найдено
          </h2>
          <p className="text-sm text-text-secondary mb-6 leading-relaxed">
            Попробуйте уточнить запрос и повторить поиск. Если запрос слишком
            широкий, сузьте тему или ослабьте фильтры.
          </p>
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-accent hover:bg-accent-muted text-white text-sm font-medium rounded-lg transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Повторить поиск
          </button>
        </div>
      </div>
    );
  }

  if (state === "error") {
    return (
      <div className="flex flex-col items-center justify-center py-20 px-4 animate-fade-in">
        <div className="max-w-md text-center">
          <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-danger-muted/30 flex items-center justify-center">
            <AlertCircle className="w-7 h-7 text-danger" />
          </div>
          <h2 className="text-lg font-semibold text-text-primary mb-2">
            Ошибка поиска
          </h2>
          <p className="text-sm text-text-secondary mb-6 leading-relaxed">
            Не удалось выполнить поиск. Проверьте доступность сервисов и
            повторите попытку.
          </p>
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-accent hover:bg-accent-muted text-white text-sm font-medium rounded-lg transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Повторить
          </button>
        </div>
      </div>
    );
  }

  if (state === "partial") {
    return (
      <div className="flex flex-col items-center justify-center py-20 px-4 animate-fade-in">
        <div className="max-w-md text-center">
          <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-warning-muted/30 flex items-center justify-center">
            <AlertCircle className="w-7 h-7 text-warning" />
          </div>
          <h2 className="text-lg font-semibold text-text-primary mb-2">
            Частичный результат
          </h2>
          <p className="text-sm text-text-secondary mb-4 leading-relaxed">
            Часть источников недоступна. Показаны результаты только из доступных
            площадок.
          </p>
          <div className="bg-bg-card border border-border-default rounded-lg p-4 mb-5">
            <h3 className="text-[11px] uppercase tracking-wider text-text-tertiary mb-2">
              Недоступные источники
            </h3>
            {sourcesFailed.map((s) => (
              <p key={s} className="text-xs text-warning">
                • {s}
              </p>
            ))}
          </div>
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-accent hover:bg-accent-muted text-white text-sm font-medium rounded-lg transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Повторить по всем источникам
          </button>
        </div>
      </div>
    );
  }

  return null;
});
