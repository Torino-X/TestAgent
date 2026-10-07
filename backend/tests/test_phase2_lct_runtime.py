from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


_RUNTIME_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phase2_lct_runtime.py"
_SPEC = importlib.util.spec_from_file_location("phase2_lct_runtime_test", _RUNTIME_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_RUNTIME = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNTIME
_SPEC.loader.exec_module(_RUNTIME)


def test_observe_records_failure_without_aborting_and_finalizes_test_fail(tmp_path: Path) -> None:
    run = _RUNTIME.LctRun(lct="TEST-OBSERVE", repo_root=tmp_path, execution_path="test")
    first = run.step("first independent oracle", component="first", expected="pass")
    second = run.step("second independent oracle", component="second", expected="pass")

    assert run.observe(first, False, actual="first failure") is False
    assert first.status == "FAIL"
    assert run.check(second, True, actual="second completed") is True

    assert run.finish(lct_verdict="LCT_PASS", classification="test") == 1
    report = json.loads((run.output_dir / "report.json").read_text(encoding="utf-8"))
    assert report["test_status"] == "TEST_FAIL"
    assert [step["status"] for step in report["steps"]] == ["FAIL", "PASS"]
