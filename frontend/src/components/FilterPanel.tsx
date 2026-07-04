import { useState, useRef, useEffect, useMemo, useId } from "react";
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

const MIN_YEAR = 1900;
const MAX_YEAR = 2026;

function validateYearRange(dateFrom: string, dateTo: string): string | null {
  const from = dateFrom === "" ? null : parseInt(dateFrom, 10);
  const to = dateTo === "" ? null : parseInt(dateTo, 10);
  const range = `от ${String(MIN_YEAR)} до ${String(MAX_YEAR)}`;

  if (dateFrom !== "" && (from === null || Number.isNaN(from))) {
    return `«От» — введите год ${range}`;
  }
  if (dateTo !== "" && (to === null || Number.isNaN(to))) {
    return `«До» — введите год ${range}`;
  }
  if (from !== null && (from < MIN_YEAR || from > MAX_YEAR)) {
    return `«От» — год ${range}`;
  }
  if (to !== null && (to < MIN_YEAR || to > MAX_YEAR)) {
    return `«До» — год ${range}`;
  }
  if (from !== null && to !== null && from > to) {
    return "«От» не может быть больше «До»";
  }
  return null;
}

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
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const labelId = useId();
  const listboxId = useId();

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const idx = Math.max(
      0,
      options.findIndex((o) => o.value === value),
    );
    const timer = window.setTimeout(() => {
      optionRefs.current[idx]?.focus();
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [open, options, value]);

  const current = options.find((o) => o.value === value);

  const focusOption = (idx: number) => {
    const n = options.length;
    if (n === 0) return;
    const wrapped = ((idx % n) + n) % n;
    optionRefs.current[wrapped]?.focus();
  };

  const selectAndClose = (idx: number) => {
    onChange(options[idx].value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const handleTriggerKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    switch (e.key) {
      case "ArrowDown":
      case "ArrowUp":
      case "Enter":
      case " ":
        e.preventDefault();
        setOpen(true);
        break;
    }
  };

  const handleOptionKeyDown = (
    e: React.KeyboardEvent<HTMLButtonElement>,
    idx: number,
  ) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        focusOption(idx + 1);
        break;
      case "ArrowUp":
        e.preventDefault();
        focusOption(idx - 1);
        break;
      case "Home":
        e.preventDefault();
        focusOption(0);
        break;
      case "End":
        e.preventDefault();
        focusOption(options.length - 1);
        break;
      case "Enter":
      case " ":
        e.preventDefault();
        selectAndClose(idx);
        break;
      case "Escape":
        e.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  };

  return (
    <div ref={containerRef} className="relative">
      <div
        id={labelId}
        className="block text-xs uppercase tracking-wider text-text-tertiary mb-1.5"
      >
        {label}
      </div>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => {
          setOpen(!open);
        }}
        onKeyDown={handleTriggerKeyDown}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={labelId}
        aria-controls={open ? listboxId : undefined}
        className="w-full min-h-[44px] flex items-center justify-between bg-bg-input border border-border-default rounded-lg px-3 py-2 text-sm text-text-primary hover:border-accent/30 transition-colors"
      >
        <span>{current?.label}</span>
        <ChevronDown
          className={`w-4 h-4 text-text-tertiary transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div
          id={listboxId}
          role="listbox"
          aria-labelledby={labelId}
          className="absolute top-full left-0 right-0 mt-1 bg-bg-elevated border border-border-default rounded-lg shadow-xl z-dropdown overflow-hidden"
        >
          {options.map((opt, i) => {
            const selected = opt.value === value;
            return (
              <button
                ref={(el) => {
                  optionRefs.current[i] = el;
                }}
                type="button"
                key={opt.value}
                role="option"
                aria-selected={selected}
                tabIndex={-1}
                onClick={() => {
                  selectAndClose(i);
                }}
                onKeyDown={(e) => {
                  handleOptionKeyDown(e, i);
                }}
                className={`w-full min-h-[44px] text-left px-3 py-2 text-sm hover:bg-bg-hover transition-colors ${
                  selected
                    ? "text-accent-text bg-accent-subtle/30"
                    : "text-text-secondary"
                }`}
              >
                {opt.label}
              </button>
            );
          })}
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

  const yearError = useMemo(
    () => validateYearRange(filters.dateFrom, filters.dateTo),
    [filters.dateFrom, filters.dateTo],
  );

  const mobilePanelRef = useRef<HTMLDivElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!isMobileOpen) {
      previousFocusRef.current?.focus();
      return;
    }
    previousFocusRef.current =
      (document.activeElement as HTMLElement | null) ?? null;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => {
      closeBtnRef.current?.focus();
    }, 0);

    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onMobileClose();
        return;
      }
      if (e.key !== "Tab") return;
      const panel = mobilePanelRef.current;
      if (!panel) return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => el.offsetParent !== null);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", handler);
      document.body.style.overflow = "";
    };
  }, [isMobileOpen, onMobileClose]);

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
        <div className="block text-xs uppercase tracking-wider text-text-tertiary mb-2.5">
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

      <fieldset className="border-0 p-0 m-0">
        <legend className="block text-xs uppercase tracking-wider text-text-tertiary mb-1.5">
          Диапазон лет
        </legend>
        <p id="year-range-help" className="text-xs text-text-tertiary mb-2">
          Год публикации, {MIN_YEAR}–{MAX_YEAR}. Оставьте пустым, чтобы не
          ограничивать.
        </p>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <label
              htmlFor="filter-date-from"
              className="block text-xs uppercase tracking-wider text-text-tertiary mb-1.5"
            >
              От
            </label>
            <input
              id="filter-date-from"
              name="dateFrom"
              type="number"
              placeholder={String(MIN_YEAR)}
              value={filters.dateFrom}
              onChange={(e) => {
                updateFilter("dateFrom", e.target.value);
              }}
              aria-describedby={
                yearError
                  ? "year-range-help year-range-error"
                  : "year-range-help"
              }
              aria-invalid={yearError ? true : undefined}
              className={`w-full bg-bg-input border rounded-lg px-3 py-2 text-sm tabular-nums text-text-primary placeholder:text-text-tertiary focus:border-accent/50 transition-colors ${
                yearError ? "border-danger/60" : "border-border-default"
              }`}
              min={String(MIN_YEAR)}
              max={String(MAX_YEAR)}
              inputMode="numeric"
            />
          </div>
          <span className="text-text-tertiary text-sm shrink-0 pb-2.5">—</span>
          <div className="flex-1">
            <label
              htmlFor="filter-date-to"
              className="block text-xs uppercase tracking-wider text-text-tertiary mb-1.5"
            >
              До
            </label>
            <input
              id="filter-date-to"
              name="dateTo"
              type="number"
              placeholder={String(MAX_YEAR)}
              value={filters.dateTo}
              onChange={(e) => {
                updateFilter("dateTo", e.target.value);
              }}
              aria-describedby={
                yearError
                  ? "year-range-help year-range-error"
                  : "year-range-help"
              }
              aria-invalid={yearError ? true : undefined}
              className={`w-full bg-bg-input border rounded-lg px-3 py-2 text-sm tabular-nums text-text-primary placeholder:text-text-tertiary focus:border-accent/50 transition-colors ${
                yearError ? "border-danger/60" : "border-border-default"
              }`}
              min={String(MIN_YEAR)}
              max={String(MAX_YEAR)}
              inputMode="numeric"
            />
          </div>
        </div>
        {yearError && (
          <p
            id="year-range-error"
            role="alert"
            aria-live="polite"
            className="mt-2 text-xs text-danger"
          >
            {yearError}
          </p>
        )}
      </fieldset>

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
        <div className="fixed inset-0 z-drawer lg:hidden">
          <button
            type="button"
            onClick={onMobileClose}
            className="absolute inset-0 bg-black/60 cursor-default"
            aria-label="Закрыть фильтры"
          />
          <div
            ref={mobilePanelRef}
            role="dialog"
            aria-modal="true"
            aria-label="Параметры поиска"
            className="absolute left-0 top-0 bottom-0 w-[320px] max-w-[85vw] bg-bg-primary border-r border-border-default overflow-y-auto animate-slide-in-left"
          >
            <div className="p-5">
              <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-2">
                  <Filter className="w-4 h-4 text-text-tertiary" />
                  <h2 className="text-sm font-medium text-text-primary">
                    Параметры
                  </h2>
                </div>
                <button
                  ref={closeBtnRef}
                  type="button"
                  onClick={onMobileClose}
                  className="min-w-[44px] min-h-[44px] flex items-center justify-center -mr-2 text-text-tertiary hover:text-text-primary transition-colors"
                  aria-label="Закрыть фильтры"
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
