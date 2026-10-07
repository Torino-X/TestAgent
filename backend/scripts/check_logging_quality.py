"""Focused static quality gate for newly written structured logging code."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.logging_system.events import EVENT_CATALOG

_PATTERNS = {
    "dangerous_print": re.compile(r"\bprint\s*\("),
    "prompt_logging": re.compile(r"(?:log_event|logger\.(?:info|warning|error)).{0,100}(?:prompt|messages|completion|authorization|cookie|api_key)", re.I),
    "dynamic_event": re.compile(r"log_event\([^\n]+(?:f[\"']|\+\s*task_id)"),
}
_ALLOWLIST = {"app/main.py"}


def check(paths: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        normalized = path.as_posix()
        if normalized.endswith(tuple(_ALLOWLIST)):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, pattern in _PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: {name}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="检查日志代码中的高风险模式")
    parser.add_argument("paths", nargs="*", default=["app/core/logging_system", "app/core/observability"])
    args = parser.parse_args()
    files = [file for raw in args.paths for file in ([Path(raw)] if Path(raw).is_file() else Path(raw).rglob("*.py"))]
    findings = check(files)
    print("\n".join(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
