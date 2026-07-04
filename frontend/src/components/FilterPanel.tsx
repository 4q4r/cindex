import { useState, useRef, useEffect } from "react";
import { ChevronDown, X, Filter, Check } from "lucide-react";
import { Filters } from "../types";

const CITATION_STYLE_OPTIONS = [
  { value: "gost2018", label: "ГОСТ Р 7.0.100-2018" },
  { value: "mla", label: "MLA" },
  { value: "apa", label: "APA" },
  { value: "vancouver", label: "Vancouver" },
  { value: "ieee", label: "IEEE" },
  { value: "harvard", label: "Harvard" },
] as const;

const SORT_OPTIONS = [
  { value: "relevance", label: "По релевантности" },
  { value: "newest", label: "Сначала новые" },
  { value: "metadata", label: "По полноте метаданных" },
] as const;

interface FilterPanelProps {
  filters: Filters;
  onFiltersChange: (f: Filters) => void;
  isMobileOpen: boolean;
  onMobileClose: () => void;
}

function SelectDropdown({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: readonly { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node))
        setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
    };
  }, []);

  const current = options.find((o) => o.value === value);

  return (
    <div ref={ref} className="relative">
      <div className="block text-[11px] uppercase tracking-wider text-text-tertiary mb-1.5">
        {label}
      </div>
      <button
        type="button"
        onClick={() => {
          setOpen(!open);
        }}
        aria-label={label}
        aria-haspopup="listbox"
        className="w-full min-h-[44px] flex items-center justify-between bg-bg-input border border-border-default rounded-lg px-3 py-2 text-sm text-text-primary hover:border-accent/30 transition-colors"
      >
        <span>{current?.label}</span>
        <ChevronDown
          className={`w-4 h-4 text-text-tertiary transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-bg-elevated border border-border-default rounded-lg shadow-xl z-50 overflow-hidden">
          {options.map((opt) => (
            <button
              type="button"
              key={opt.value}
              onClick={() => {
                onChange(opt.value);
                setOpen(false);
              }}
              className={`w-full min-h-[44px] text-left px-3 py-2 text-sm hover:bg-bg-hover transition-colors ${
                opt.value === value
                  ? "text-accent bg-accent-subtle/30"
                  : "text-text-secondary"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CheckboxItem({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => {
        onChange(!checked);
      }}
      className="flex min-h-[44px] items-center gap-2.5 cursor-pointer group py-2 text-left"
    >
      <div
        className={`w-4 h-4 rounded border flex items-center justify-center transition-colors shrink-0 ${
          checked
            ? "bg-accent border-accent"
            : "border-border-default group-hover:border-text-tertiary"
        }`}
      >
        {checked && <Check className="w-3 h-3 text-white" />}
      </div>
      <span className="text-sm text-text-secondary group-hover:text-text-primary transition-colors select-none">
        {label}
      </span>
    </button>
  );
}

export function FilterPanel({
  filters,
  onFiltersChange,
  isMobileOpen,
  onMobileClose,
}: FilterPanelProps) {
  const updateFilter = <K extends keyof Filters>(key: K, value: Filters[K]) => {
    onFiltersChange({ ...filters, [key]: value });
  };

  const content = (
    <div className="space-y-6">
      <SelectDropdown
        label="Стиль цитирования"
        value={filters.citationStyle}
        options={CITATION_STYLE_OPTIONS}
        onChange={(v) => {
          updateFilter(
            "citationStyle",
            v as "gost2018" | "mla" | "apa" | "vancouver" | "ieee" | "harvard",
          );
        }}
      />

      <div>
        <div className="block text-[11px] uppercase tracking-wider text-text-tertiary mb-2.5">
          Строгие фильтры
        </div>
        <div className="space-y-0.5">
          <CheckboxItem
            label="Только peer-reviewed/refereed"
            checked={filters.peerReviewedOnly}
            onChange={(v) => {
              updateFilter("peerReviewedOnly", v);
            }}
          />
          <CheckboxItem
            label="Только индексируемые (Scopus/WoS и др.)"
            checked={filters.indexedOnly}
            onChange={(v) => {
              updateFilter("indexedOnly", v);
            }}
          />
          <CheckboxItem
            label="Исключить preprint и author manuscript"
            checked={filters.excludePreprints}
            onChange={(v) => {
              updateFilter("excludePreprints", v);
            }}
          />
        </div>
      </div>

      <div>
        <div className="block text-[11px] uppercase tracking-wider text-text-tertiary mb-1.5">
          Диапазон лет
        </div>
        <div className="flex items-center gap-2">
          <input
            id="filter-date-from"
            name="dateFrom"
            type="number"
            placeholder="От"
            value={filters.dateFrom}
            onChange={(e) => {
              updateFilter("dateFrom", e.target.value);
            }}
            aria-label="Дата с"
            className="w-full bg-bg-input border border-border-default rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus:border-accent/50 transition-colors"
            min="1900"
            max="2026"
          />
          <span className="text-text-tertiary text-sm shrink-0">—</span>
          <input
            id="filter-date-to"
            name="dateTo"
            type="number"
            placeholder="До"
            value={filters.dateTo}
            onChange={(e) => {
              updateFilter("dateTo", e.target.value);
            }}
            aria-label="Дата до"
            className="w-full bg-bg-input border border-border-default rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus:border-accent/50 transition-colors"
            min="1900"
            max="2026"
          />
        </div>
      </div>

      <SelectDropdown
        label="Сортировка"
        value={filters.sortBy}
        options={SORT_OPTIONS}
        onChange={(v) => {
          updateFilter("sortBy", v as "relevance" | "newest" | "metadata");
        }}
      />
    </div>
  );

  return (
    <>
      <aside className="hidden lg:block w-[280px] shrink-0">
        <div className="sticky top-6 bg-bg-card border border-border-default rounded-xl p-5">
          <div className="flex items-center gap-2 mb-5">
            <Filter className="w-4 h-4 text-text-tertiary" />
            <h2 className="text-sm font-medium text-text-primary">Параметры</h2>
          </div>
          {content}
        </div>
      </aside>

      {isMobileOpen && (
        <div className="fixed inset-0 z-[60] lg:hidden">
          <div
            className="absolute inset-0 bg-black/60"
            onClick={onMobileClose}
            onKeyDown={(e) => {
              if (e.key === "Escape") onMobileClose();
            }}
            role="button"
            tabIndex={0}
            aria-label="Закрыть фильтры"
          />
          <div className="absolute left-0 top-0 bottom-0 w-[320px] max-w-[85vw] bg-bg-primary border-r border-border-default overflow-y-auto animate-slide-in-left">
            <div className="p-5">
              <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-2">
                  <Filter className="w-4 h-4 text-text-tertiary" />
                  <h2 className="text-sm font-medium text-text-primary">
                    Параметры
                  </h2>
                </div>
                <button
                  type="button"
                  onClick={onMobileClose}
                  className="min-w-[44px] min-h-[44px] flex items-center justify-center -mr-2 text-text-tertiary hover:text-text-primary transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
              {content}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
