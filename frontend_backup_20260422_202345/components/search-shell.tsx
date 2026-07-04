"use client";

import { useMemo, useState, useTransition } from "react";

type SearchResult = {
  id: number;
  title: string;
  citation_full: string;
  snippet: string;
  year: number | null;
  source: string;
  url: string;
  identifiers: Record<string, string>;
  eligibility_evidence: {
    peer_reviewed: boolean;
    indexed: boolean;
    doi_and_journal_card: boolean;
    not_preprint: boolean;
  };
};

const labels = {
  ru: {
    title: "Академический поиск источников",
    subtitle: "Ищите peer-reviewed статьи и сразу берите цитирование и доказательные фрагменты.",
    placeholder: "Тема, ключевики, термин...",
    search: "Искать",
    style: "Стиль цитирования",
    sourceOpen: "Открыть источник",
    copyCitation: "Копировать цитирование",
    copySnippet: "Копировать слайс",
    researchQuery: "Исследовательский запрос",
    lang: "Язык интерфейса",
    filters: "Параметры",
    resultList: "Список найденных источников",
    checklist: "Проверка критериев",
    count: "Результатов",
    noResults: "Результаты не найдены",
    noResultsHint: "Уточните тему, добавьте более конкретные термины или смените формулировку запроса.",
    loading: "Идет поиск по источникам...",
  },
  en: {
    title: "Scholarly Source Search",
    subtitle: "Find peer-reviewed sources and copy citation-ready evidence snippets.",
    placeholder: "Topic, keywords, terms...",
    search: "Search",
    style: "Citation style",
    sourceOpen: "Open source",
    copyCitation: "Copy citation",
    copySnippet: "Copy snippet",
    researchQuery: "Research query",
    lang: "Interface language",
    filters: "Parameters",
    resultList: "Found sources",
    checklist: "Eligibility checks",
    count: "Results",
    noResults: "No results",
    noResultsHint: "Try a narrower query, add concrete terms, or rephrase your search.",
    loading: "Searching across sources...",
  },
} as const;

export function SearchShell() {
  const [query, setQuery] = useState("");
  const [style, setStyle] = useState<"gost_2018" | "gost_2003">("gost_2018");
  const [lang, setLang] = useState<"ru" | "en">("ru");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searched, setSearched] = useState(false);
  const [isPending, startTransition] = useTransition();

  const t = useMemo(() => labels[lang], [lang]);

  const onSearch = () => {
    if (!query.trim()) return;
    setSearched(true);
    startTransition(async () => {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"}/api/v1/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, style }),
      });
      if (!response.ok) {
        setResults([]);
        return;
      }
      const payload = await response.json();
      setResults(payload.results ?? []);
    });
  };

  return (
    <main className="search-page">
      <section className="shell-head">
        <div className="brand-lockup">
          <p className="brand-mark">CIndex</p>
          <h1>{t.title}</h1>
          <p>{t.subtitle}</p>
        </div>
        <div className="head-meta">
          <span>{t.count}: {results.length}</span>
          <span>API: /api/v1/search</span>
        </div>
      </section>

      <section className="workspace" aria-label={t.researchQuery}>
        <div className="workspace-main">
          <label className="field-label" htmlFor="query-input">{t.researchQuery}</label>
          <div className="query-row">
            <input
              id="query-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t.placeholder}
              onKeyDown={(e) => (e.key === "Enter" ? onSearch() : null)}
            />
            <button onClick={onSearch} disabled={isPending}>{isPending ? "..." : t.search}</button>
          </div>
        </div>
        <div className="workspace-side" aria-label={t.filters}>
          <label className="field-label" htmlFor="lang-select">{t.lang}</label>
          <select id="lang-select" value={lang} onChange={(e) => setLang(e.target.value as "ru" | "en")}>
            <option value="ru">RU</option>
            <option value="en">EN</option>
          </select>
          <label className="field-label" htmlFor="style-select">{t.style}</label>
          <select id="style-select" value={style} onChange={(e) => setStyle(e.target.value as "gost_2018" | "gost_2003")}>
            <option value="gost_2018">ГОСТ Р 7.0.100-2018</option>
            <option value="gost_2003">ГОСТ 7.1-2003</option>
          </select>
        </div>
      </section>

      <section className="results-panel" aria-live="polite" aria-label={t.resultList}>
        <header className="results-head">
          <h2>{t.resultList}</h2>
          {isPending ? <span>{t.loading}</span> : <span>{t.count}: {results.length}</span>}
        </header>
        {results.length === 0 && !isPending && searched ? (
          <div className="empty-state">
            <p>{t.noResults}</p>
            <p>{t.noResultsHint}</p>
          </div>
        ) : null}
        {results.map((item) => (
          <article key={item.id} className="result-item">
            <header className="result-top">
              <h2>{item.title}</h2>
              <p>{item.source} · {item.year ?? "n.d."}</p>
            </header>
            <p className="snippet">{item.snippet}</p>
            <p className="citation"><strong>{t.style}:</strong> {item.citation_full}</p>
            <div className="meta-list">
              {Object.entries(item.identifiers).map(([k, v]) => <span key={k}>{k.toUpperCase()}: {v}</span>)}
            </div>
            <div className="checks" aria-label={t.checklist}>
              <span data-on={item.eligibility_evidence.peer_reviewed}>peer-reviewed</span>
              <span data-on={item.eligibility_evidence.indexed}>indexed</span>
              <span data-on={item.eligibility_evidence.doi_and_journal_card}>doi+journal card</span>
              <span data-on={item.eligibility_evidence.not_preprint}>not preprint</span>
            </div>
            <div className="actions-row">
              <button onClick={() => navigator.clipboard.writeText(item.citation_full)}>{t.copyCitation}</button>
              <button onClick={() => navigator.clipboard.writeText(item.snippet)}>{t.copySnippet}</button>
              <a href={item.url} target="_blank" rel="noreferrer">{t.sourceOpen}</a>
            </div>
          </article>
        ))}
      </section>
    </main>
  );
}
