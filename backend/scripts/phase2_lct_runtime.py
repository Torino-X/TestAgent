"""Shared runtime for newly authored Phase 2 resource-pack scenarios.

This module deliberately does not import or execute any existing pytest test.
Every LCT script records its own inputs, assertions and diagnostic evidence.
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


VALID_LCT_VERDICTS = {
    "LCT_PASS",
    "LCT_PARTIAL_PASS",
    "AUTOMATED_ONLY",
    "OBSERVABILITY_GAP",
    "NOT_IMPLEMENTED",
    "INVALID_TEST_ASSUMPTION",
    "TEST_FAIL",
}


def _json_default(value: Any) -> str:
    return str(value)


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # pragma: no cover - diagnostic only
        return None


@dataclass
class Step:
    name: str
    component: str
    expected: str
    actual: str | None = None
    status: str = "PENDING"
    inputs: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, str] | None = None


class LctRun:
    """Writes portable evidence even when a scenario crashes midway."""

    def __init__(self, *, lct: str, repo_root: Path, execution_path: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.lct = lct
        self.execution_path = execution_path
        self.output_dir = repo_root / "test-results" / "phase2-resource-pack" / stamp / lct
        self.evidence_dir = self.output_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = repo_root
        self.steps: list[Step] = []
        self.classification: str | None = None
        self.notes: list[str] = []
        self._log_lines: list[str] = []

    def log(self, message: str) -> None:
        """Mirror concise execution events into the result bundle.

        Console events use the machine's local time so they can be compared
        directly with local uvicorn logs.  The report keeps its canonical UTC
        timestamp separately for portable evidence bundles.
        """
        line = f"{datetime.now().astimezone().isoformat()} {message}"
        self._log_lines.append(line)
        print(line, flush=True)

    def step(self, name: str, *, component: str, expected: str, inputs: dict[str, Any] | None = None) -> Step:
        step = Step(name=name, component=component, expected=expected, inputs=inputs or {})
        self.steps.append(step)
        return step

    def check(
        self,
        step: Step,
        condition: bool,
        *,
        actual: str,
        evidence: dict[str, Any] | None = None,
        fail_fast: bool = True,
    ) -> bool:
        """Record one oracle result and optionally stop the scenario.

        Resource-pack runners historically used CI-style fail-fast assertions.
        Expensive live-model scenarios can instead set ``fail_fast=False`` to
        collect independent downstream evidence in the same authenticated
        conversation.  The final report remains TEST_FAIL whenever any such
        observation failed.
        """
        step.actual = actual
        step.evidence.update(evidence or {})
        step.status = "PASS" if condition else "FAIL"
        if not condition:
            message = f"{step.name}: expected {step.expected}; actual {actual}"
            failure_file = self.evidence_dir / f"{len(self.steps):02d}-{step.name.replace(' ', '_')}-failure.txt"
            failure_file.write_text(message + "\n", encoding="utf-8")
            step.failure = {
                "type": "AssertionError",
                "message": message,
                "traceback_file": str(failure_file),
            }
            if fail_fast:
                raise AssertionError(message)
        return condition

    def observe(
        self,
        step: Step,
        condition: bool,
        *,
        actual: str,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Persist a non-terminal oracle failure for live diagnostic runs."""
        return self.check(
            step,
            condition,
            actual=actual,
            evidence=evidence,
            fail_fast=False,
        )

    def capture(self, step: Step, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
            step.status = "PASS"
            step.actual = "completed"
            return value
        except Exception as exc:
            step.status = "FAIL"
            step.actual = f"{type(exc).__name__}: {exc}"
            failure_file = self.evidence_dir / f"{len(self.steps):02d}-{step.name.replace(' ', '_')}-failure.txt"
            failure_file.write_text(traceback.format_exc(), encoding="utf-8")
            step.failure = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback_file": str(failure_file),
            }
            raise

    def write_evidence(self, name: str, value: Any) -> str:
        path = self.evidence_dir / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        return str(path)

    def finish(self, *, lct_verdict: str, classification: str, notes: list[str] | None = None) -> int:
        if lct_verdict not in VALID_LCT_VERDICTS:
            raise ValueError(f"Unsupported LCT verdict: {lct_verdict}")
        self.classification = classification
        self.notes.extend(notes or [])
        failed = [step for step in self.steps if step.status == "FAIL"]
        report = {
            "schema": "phase2-resource-pack-lct/v1",
            "lct": self.lct,
            "test_status": "TEST_FAIL" if failed else "TEST_PASS",
            "lct_verdict": "TEST_FAIL" if failed else lct_verdict,
            "execution_path": self.execution_path,
            "classification": classification,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "timestamp_local": datetime.now().astimezone().isoformat(),
            "branch": _git(self.repo_root, "branch", "--show-current"),
            "head": _git(self.repo_root, "rev-parse", "--short", "HEAD"),
            "steps": [step.__dict__ for step in self.steps],
            "failed_step": failed[0].name if failed else None,
            "notes": self.notes,
        }
        (self.output_dir / "steps.json").write_text(
            json.dumps(report["steps"], ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
        )
        (self.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
        )
        (self.output_dir / "execution.log").write_text(
            "\n".join(self._log_lines) + "\n", encoding="utf-8"
        )
        print(json.dumps({"lct": self.lct, "test_status": report["test_status"], "lct_verdict": report["lct_verdict"], "report": str(self.output_dir / "report.json")}, ensure_ascii=False))
        return 1 if failed else 0


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cli_failure(run: LctRun, exc: Exception, *, classification: str) -> int:
    if not any(step.status == "FAIL" for step in run.steps):
        synthetic = run.step("unhandled scenario failure", component="scenario runtime", expected="scenario completes")
        synthetic.status = "FAIL"
        synthetic.actual = f"{type(exc).__name__}: {exc}"
        failure_file = run.evidence_dir / "unhandled-failure.txt"
        failure_file.write_text(traceback.format_exc(), encoding="utf-8")
        synthetic.failure = {"type": type(exc).__name__, "message": str(exc), "traceback_file": str(failure_file)}
    return run.finish(lct_verdict="TEST_FAIL", classification=classification)
