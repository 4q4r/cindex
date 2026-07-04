from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class QueryResult:
    query: str
    count: int
    elapsed_ms: int
    error: str = ""


@dataclass
class SourceReport:
    source_key: str
    results: list[QueryResult] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        return sum(x.count for x in self.results)

    @property
    def errors(self) -> list[str]:
        return [x.error for x in self.results if x.error]


def build_report(per_source_limit: int) -> dict:
    from apps.ingestion.connectors import CONNECTORS
    from apps.ingestion.live_queries import REQUIRED_SOURCES, SOURCE_QUERY_MATRIX

    reports: list[SourceReport] = []
    for source_key in REQUIRED_SOURCES:
        connector = CONNECTORS[source_key]()
        source_report = SourceReport(source_key=source_key)
        for query in SOURCE_QUERY_MATRIX[source_key]:
            started = time.perf_counter()
            try:
                items = connector.fetch(query, limit=per_source_limit)
                error = ""
                count = len(items)
            except (ValueError, RuntimeError, ConnectionError) as exc:
                # pragma: no cover - network dependent
                error = str(exc)
                count = 0
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            source_report.results.append(
                QueryResult(
                    query=query, count=count, elapsed_ms=elapsed_ms, error=error,
                ),
            )
        reports.append(source_report)

    payload = {
        "generated_at": int(time.time()),
        "per_source_limit": per_source_limit,
        "sources": [
            {
                "source_key": r.source_key,
                "total_items": r.total_items,
                "errors": r.errors,
                "results": [asdict(x) for x in r.results],
            }
            for r in reports
        ],
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tmp/live_smoke_report.json")
    parser.add_argument("--per-source-limit", type=int, default=3)
    parser.add_argument("--strict-errors", action="store_true")
    args = parser.parse_args()

    report = build_report(per_source_limit=args.per_source_limit)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    has_errors = any(x["errors"] for x in report["sources"])
    if args.strict_errors and has_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
