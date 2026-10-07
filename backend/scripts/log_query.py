"""Read and filter local TestAgent JSONL logs without touching application data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query local TestAgent structured JSONL logs")
    parser.add_argument("--log-dir", default="./logs", help="Logging root directory (default: ./logs)")
    for field in ("task-id", "trace-id", "request-id", "conversation-id", "project-id", "event", "level"):
        parser.add_argument(f"--{field}")
    parser.add_argument("--tail", type=int, default=0, help="Return only the last N matching records")
    return parser.parse_args()


def load_records(log_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((log_dir / "app").glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        except OSError:
            continue
    return records


def matches(record: dict[str, Any], args: argparse.Namespace) -> bool:
    mapping = {
        "task_id": args.task_id,
        "trace_id": args.trace_id,
        "request_id": args.request_id,
        "conversation_id": args.conversation_id,
        "project_id": args.project_id,
        "event": args.event,
        "level": args.level.upper() if args.level else None,
    }
    return all(expected is None or str(record.get(field)) == expected for field, expected in mapping.items())


def main() -> int:
    args = parse_args()
    records = [record for record in load_records(Path(args.log_dir)) if matches(record, args)]
    records.sort(key=lambda record: str(record.get("timestamp", "")))
    if args.tail > 0:
        records = records[-args.tail:]
    for record in records:
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
