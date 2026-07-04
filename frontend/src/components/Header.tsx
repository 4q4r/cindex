import { memo } from "react";
import { BookOpen, Clock, Radio } from "lucide-react";
import { SearchState } from "../types";

interface HeaderProps {
  resultCount: number;
  lastSearchTime: Date | null;
  sourcesQueried: number;
  sourcesFailed: string[];
  searchState: SearchState;
}

export const Header = memo(function Header({
  resultCount,
  lastSearchTime,
  sourcesQueried,
  sourcesFailed,
  searchState,
}: HeaderProps) {
  const formatTime = (date: Date) => {
    return date.toLocaleTimeString("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  const activeSources = sourcesQueried - sourcesFailed.length;

  return (
    <header className="border-b border-border-default bg-bg-primary/95 backdrop-blur-sm">
      <div className="max-w-[1440px] mx-auto px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <BookOpen className="w-5 h-5 text-accent" />
            <h1 className="text-lg font-semibold tracking-tight text-text-primary">
              CIndex
            </h1>
          </div>
          <span className="text-xs text-text-tertiary border-l border-border-default pl-3">
            Поиск источников для цитирования
          </span>
        </div>

        <div className="flex items-center gap-5 text-xs text-text-secondary">
          <div className="flex items-center gap-1.5">
            <Radio className="w-3.5 h-3.5" />
            <span
              className={`tabular-nums ${
                sourcesFailed.length > 0 ? "text-warning" : "text-success"
              }`}
            >
              {searchState === "idle"
                ? `доступно ${String(activeSources)} источников`
                : `активно ${String(activeSources)} из ${String(sourcesQueried)}`}
            </span>
          </div>

          {searchState !== "idle" && (
            <>
              <div className="flex items-center gap-1.5">
                <BookOpen className="w-3.5 h-3.5" />
                <span className="tabular-nums">Найдено: {resultCount}</span>
              </div>

              {lastSearchTime && (
                <div className="flex items-center gap-1.5">
                  <Clock className="w-3.5 h-3.5" />
                  <span className="tabular-nums">
                    Поиск: {formatTime(lastSearchTime)}
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </header>
  );
});
