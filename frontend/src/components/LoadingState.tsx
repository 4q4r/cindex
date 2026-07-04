import { memo } from "react";
import { SearchProgress } from "../types";
import {
  CheckCircle2,
  Clock3,
  Search,
  FileSearch,
  ScanSearch,
} from "lucide-react";

interface LoadingStateProps {
  progress: SearchProgress | null;
}

type StageKey = SearchProgress["stage"];
type KnownStageKey = Exclude<StageKey, "queued">;

interface StageMeta {
  title: string;
  description: string;
  range: string;
  icon: typeof Search;
}

const STAGE_META: Record<KnownStageKey | "default", StageMeta> = {
  default: {
    title: "Подготовка поиска",
    description: "Готовим запрос и проверяем, с чего начать поиск.",
    range: "0%",
    icon: Clock3,
  },
  checking_index: {
    title: "Проверяем готовые результаты",
    description: "Сначала смотрим, нет ли уже подходящих статей в корпусе.",
    range: "0–20%",
    icon: Search,
  },
  live_scan: {
    title: "Собираем статьи",
    description:
      "Если в корпусе пусто или данные устарели, идём напрямую в источники и собираем статьи.",
    range: "20–55%",
    icon: ScanSearch,
  },
  searching_index: {
    title: "Собираем выдачу",
    description:
      "Ранжируем найденные статьи и подготавливаем карточки для показа.",
    range: "55–100%",
    icon: FileSearch,
  },
  completed: {
    title: "Готово",
    description: "Выдача собрана и готова к просмотру.",
    range: "100%",
    icon: CheckCircle2,
  },
  failed: {
    title: "Поиск прерван",
    description: "Не удалось завершить поиск. Можно повторить попытку.",
    range: "100%",
    icon: Clock3,
  },
};

const STAGE_STEPS = [
  {
    title: "Проверка корпуса",
    range: "0–20%",
    icon: Search,
    description: "Смотрим, есть ли уже подходящая выдача.",
  },
  {
    title: "Сбор статей",
    range: "20–55%",
    icon: ScanSearch,
    description: "Проверяем площадки напрямую, если корпуса недостаточно.",
  },
  {
    title: "Ранжирование и сборка",
    range: "55–100%",
    icon: FileSearch,
    description: "Собираем карточки и сортируем их по смысловой релевантности.",
  },
] as const;

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) {
    return "—";
  }
  const rounded = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(rounded / 60);
  const remainingSeconds = rounded % 60;
  if (minutes === 0) {
    return `${String(remainingSeconds)} с`;
  }
  if (remainingSeconds === 0) {
    return `${String(minutes)} мин`;
  }
  return `${String(minutes)} мин ${String(remainingSeconds)} с`;
}

function stageMeta(stage: SearchProgress["stage"] | undefined): StageMeta {
  if (stage && stage in STAGE_META) {
    return STAGE_META[stage as KnownStageKey];
  }
  return STAGE_META.default;
}

export const LoadingState = memo(function LoadingState({
  progress,
}: LoadingStateProps) {
  const percent = progress?.percent ?? 0;
  const stage = stageMeta(progress?.stage);
  const sourceInfo =
    progress && progress.sourceTotal > 0
      ? `${String(progress.sourceDone)}/${String(progress.sourceTotal)}`
      : "—";
  const StepIcon = stage.icon;
  const substageLabel = progress?.substageLabel.trim() ?? "";
  const currentStep =
    progress?.stage === "checking_index"
      ? 1
      : progress?.stage === "live_scan"
        ? 2
        : progress?.stage === "searching_index"
          ? 3
          : progress?.stage === "completed" || progress?.stage === "failed"
            ? 3
            : 0;

  const scanReason =
    progress?.rescanTriggered && progress.rescanReason === "empty_index_hits"
      ? "Корпус пустой, поэтому ищем напрямую по источникам."
      : progress?.rescanTriggered &&
          progress.rescanReason === "stale_query_scan"
        ? "Результаты в корпусе давно не обновлялись, поэтому проверяем источники заново."
        : progress?.rescanTriggered &&
            progress.rescanReason === "forced_by_user"
          ? "Запущено принудительное обновление результатов."
          : "";
  const averageWaitWithoutEnrichment = formatDuration(
    progress?.averageWaitWithoutEnrichmentSeconds,
  );
  const averageWaitWithEnrichment = formatDuration(
    progress?.averageWaitWithEnrichmentSeconds,
  );
  const averageWaitLabel =
    averageWaitWithoutEnrichment === "—" && averageWaitWithEnrichment === "—"
      ? ""
      : `Среднее ожидание: без обогащения ${averageWaitWithoutEnrichment} · с обогащением ${averageWaitWithEnrichment}`;

  return (
    <div className="animate-fade-in">
      <div className="mb-6 rounded-2xl border border-border-default bg-bg-card/70 p-5">
        <div className="flex items-start justify-between gap-4 mb-3">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent/10 text-accent">
              <StepIcon className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-text-primary">
                  {stage.title}
                </span>
                {currentStep > 0 && (
                  <span className="rounded-full border border-border-default px-2 py-0.5 text-[11px] text-text-tertiary">
                    этап {currentStep}/3
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs text-text-secondary">
                {stage.description}
              </p>
              {substageLabel && (
                <div className="mt-2 inline-flex rounded-full border border-border-default bg-bg-elevated px-2.5 py-1 text-[11px] font-medium text-text-secondary">
                  Подстадия: {substageLabel}
                </div>
              )}
            </div>
          </div>
          <div className="text-right text-xs text-text-tertiary">
            <div>
              {Math.round(percent)}% · {stage.range}
            </div>
            <div>Источники: {sourceInfo}</div>
          </div>
        </div>
        <div className="w-full h-1 bg-bg-elevated rounded-full overflow-hidden">
          <div
            className="h-full bg-accent rounded-full transition-all duration-300"
            style={{ width: `${String(percent)}%` }}
          />
        </div>
        <div className="mt-3 space-y-1.5 text-xs">
          {progress?.message && (
            <p className="text-text-secondary">{progress.message}</p>
          )}
          {scanReason && <p className="text-text-tertiary">{scanReason}</p>}
          {averageWaitLabel && (
            <p className="text-text-tertiary">{averageWaitLabel}</p>
          )}
          <p className="text-text-tertiary">
            Общий прогресс складывается из проверки корпуса, сбора статей и
            финального ранжирования.
          </p>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        {STAGE_STEPS.map((step, index) => {
          const isActive = currentStep === index + 1;
          const isDone = currentStep > index + 1;
          const Step = step.icon;
          return (
            <div
              key={step.title}
              className={`rounded-xl border p-4 transition-colors ${
                isActive
                  ? "border-accent/40 bg-accent/5"
                  : isDone
                    ? "border-border-default bg-bg-card"
                    : "border-border-default bg-bg-card/60"
              }`}
            >
              <div className="flex items-start gap-3">
                <div
                  className={`mt-0.5 flex h-8 w-8 items-center justify-center rounded-lg ${
                    isActive
                      ? "bg-accent text-white"
                      : "bg-bg-elevated text-text-secondary"
                  }`}
                >
                  <Step className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="text-sm font-medium text-text-primary">
                      {step.title}
                    </h3>
                    <span className="text-[11px] text-text-tertiary">
                      {step.range}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-text-secondary">
                    {step.description}
                  </p>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-4 grid gap-4">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="bg-bg-card border border-border-default rounded-xl p-6"
          >
            <div className="flex items-start justify-between mb-3">
              <div className="flex-1">
                <div className="skeleton h-5 w-full max-w-lg mb-2" />
                <div className="skeleton h-5 w-3/4 max-w-md" />
              </div>
              <div className="skeleton h-5 w-20 shrink-0 ml-3" />
            </div>
            <div className="skeleton h-4 w-80 mb-3" />
            <div className="flex gap-2 mb-4">
              <div className="skeleton h-5 w-16" />
              <div className="skeleton h-5 w-24" />
              <div className="skeleton h-5 w-20" />
              <div className="skeleton h-5 w-24" />
            </div>
            <div className="space-y-2 mb-4">
              <div className="skeleton h-3.5 w-full" />
              <div className="skeleton h-3.5 w-full" />
              <div className="skeleton h-3.5 w-5/6" />
              <div className="skeleton h-3.5 w-4/5" />
              <div className="skeleton h-3.5 w-full" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
});
