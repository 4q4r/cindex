import { Search, X } from "lucide-react";

interface SearchBarProps {
  query: string;
  onQueryChange: (q: string) => void;
  onSearch: () => void;
  isLoading: boolean;
}

export function SearchBar({
  query,
  onQueryChange,
  onSearch,
  isLoading,
}: SearchBarProps) {
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !isLoading) {
      onSearch();
    }
  };

  return (
    <div className="sticky top-0 z-sticky bg-bg-primary/90 backdrop-blur-sm border-b border-border-subtle">
      <div className="px-6 py-4">
        <div className="max-w-3xl">
          <div className="relative flex items-center gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-text-tertiary" />
              <input
                id="search-query"
                name="query"
                type="text"
                value={query}
                onChange={(e) => {
                  onQueryChange(e.target.value);
                }}
                onKeyDown={handleKeyDown}
                placeholder="Тема, ключевики, DOI, автор..."
                aria-label="Тема, ключевики, DOI, автор"
                className="w-full bg-bg-input border border-border-default rounded-lg pl-12 pr-10 py-3.5 text-text-primary placeholder:text-text-tertiary focus:border-accent/50 transition-colors text-[15px]"
                disabled={isLoading}
              />
              {query && (
                <button
                  type="button"
                  onClick={() => {
                    onQueryChange("");
                  }}
                  className="absolute right-3 top-1/2 -translate-y-1/2 p-1 text-text-tertiary hover:text-text-secondary transition-colors before:absolute before:inset-[-10px] before:content-[''] before:rounded-lg"
                  aria-label="Очистить запрос"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={onSearch}
              disabled={isLoading || !query.trim()}
              className="press-feedback px-6 py-3.5 bg-accent hover:bg-accent-muted disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium rounded-lg transition-colors text-sm shrink-0"
            >
              {isLoading ? "Поиск..." : "Искать"}
            </button>
          </div>

          <div className="flex items-center gap-4 mt-2 text-[11px] text-text-tertiary">
            <span>&quot;точная фраза&quot; для точного совпадения</span>
            <span className="text-border-default">|</span>
            <span>AND / OR для булевой логики</span>
            <span className="text-border-default">|</span>
            <span>-термин для исключения</span>
          </div>
        </div>
      </div>
    </div>
  );
}
