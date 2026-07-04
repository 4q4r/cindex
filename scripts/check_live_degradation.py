from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--previous", required=True)
    args = parser.parse_args()

    current = _load(args.current)
    previous = _load(args.previous)

    curr_by_source = {x["source_key"]: x for x in current.get("sources", [])}
    prev_by_source = {x["source_key"]: x for x in previous.get("sources", [])}

    degraded: list[str] = []
    for source_key, prev in prev_by_source.items():
        prev_items = int(prev.get("total_items", 0))
        curr_items = int(curr_by_source.get(source_key, {}).get("total_items", 0))
        if prev_items > 0 and curr_items == 0:
            degraded.append(source_key)

    if degraded:
        msg = f"Degraded sources (prev>0, now=0): {', '.join(sorted(degraded))}"
        raise SystemExit(
            msg,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
