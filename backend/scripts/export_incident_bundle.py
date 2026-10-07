"""Export a sanitized, read-only incident bundle from local JSONL logs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.logging_system.redaction import redact_value

_FILTER_FIELDS = ("task_id", "trace_id", "request_id")
_FORBIDDEN = ("prompt", "messages", "completion", "document", "memory", "cookie", "authorization", "api_key", "password", "token")


def _records(log_dir: Path, filters: dict[str, str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("**/*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and all(str(record.get(key, "")) == value for key, value in filters.items()):
                output.append(redact_value(record))
    return output


def export_bundle(*, log_dir: Path, output: Path, filters: dict[str, str]) -> Path:
    records = _records(log_dir, filters)
    safe_records = [{key: value for key, value in record.items() if not any(token in key.lower() for token in _FORBIDDEN)} for record in records]
    references = {
        "agent_event_refs": sorted({str(record[key]) for record in safe_records for key in ("agent_event_id",) if record.get(key)}),
        "context_snapshot_ids": sorted({str(record[key]) for record in safe_records for key in ("snapshot_id", "snapshot_public_id") if record.get(key)}),
        "retrieval_run_ids": sorted({str(record[key]) for record in safe_records for key in ("retrieval_run_id",) if record.get(key)}),
    }
    manifest = {
        "bundle_version": "1.0", "filters": filters, "log_count": len(safe_records),
        "trace_ids": sorted({str(record["trace_id"]) for record in safe_records if record.get("trace_id")}),
        "error_fingerprints": sorted({str(record["error_fingerprint"]) for record in safe_records if record.get("error_fingerprint")}),
        "service_version": _git_revision(), "references": references,
    }
    errors = [record for record in safe_records if str(record.get("level", "")).upper() in {"ERROR", "CRITICAL"}]
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("timeline.jsonl", "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in safe_records))
        archive.writestr("errors.json", json.dumps(errors, ensure_ascii=False, indent=2))
        archive.writestr("references.json", json.dumps(references, ensure_ascii=False, indent=2))
    return output


def _git_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, timeout=3).strip()
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="导出脱敏后的日志事故包（仅限内部支持使用）")
    parser.add_argument("--log-dir", default="./logs")
    parser.add_argument("--output", default="")
    group = parser.add_mutually_exclusive_group(required=True)
    for field in _FILTER_FIELDS:
        group.add_argument(f"--{field.replace('_', '-')}")
    args = parser.parse_args()
    filters = {field: value for field in _FILTER_FIELDS if (value := getattr(args, field))}
    identifier = next(iter(filters.values())).replace("/", "_")
    output = Path(args.output) if args.output else Path(f"incident-{identifier}.zip")
    export_bundle(log_dir=Path(args.log_dir), output=output, filters=filters)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
