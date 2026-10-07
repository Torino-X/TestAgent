"""Regression checks for black-box Phase 2 scenario-runner arithmetic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "context_scenarios"
    / "long_conversation_waterlines.py"
)
_SPEC = importlib.util.spec_from_file_location("long_conversation_waterlines", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)

_PROJECT_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "context_scenarios"
    / "project_refund_long_memory.py"
)
_PROJECT_SPEC = importlib.util.spec_from_file_location("project_refund_long_memory", _PROJECT_RUNNER_PATH)
assert _PROJECT_SPEC is not None and _PROJECT_SPEC.loader is not None
_PROJECT_RUNNER = importlib.util.module_from_spec(_PROJECT_SPEC)
_PROJECT_SPEC.loader.exec_module(_PROJECT_RUNNER)

_PRESSURE_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "context_scenarios"
    / "project_context_pressure_50.py"
)
_PRESSURE_SPEC = importlib.util.spec_from_file_location(
    "project_context_pressure_50", _PRESSURE_RUNNER_PATH
)
assert _PRESSURE_SPEC is not None and _PRESSURE_SPEC.loader is not None
_PRESSURE_RUNNER = importlib.util.module_from_spec(_PRESSURE_SPEC)
_PRESSURE_SPEC.loader.exec_module(_PRESSURE_RUNNER)

_PRESSURE_WRAPPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_ctx_project_50.ps1"
)

_LIFECYCLE_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "context_scenarios"
    / "conversation_compaction_lifecycle.py"
)
_LIFECYCLE_SPEC = importlib.util.spec_from_file_location(
    "conversation_compaction_lifecycle", _LIFECYCLE_RUNNER_PATH
)
assert _LIFECYCLE_SPEC is not None and _LIFECYCLE_SPEC.loader is not None
_LIFECYCLE_RUNNER = importlib.util.module_from_spec(_LIFECYCLE_SPEC)
_LIFECYCLE_SPEC.loader.exec_module(_LIFECYCLE_RUNNER)

_LIFECYCLE_WRAPPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_ctx_compaction_lifecycle.ps1"
)


def test_long_conversation_runner_uses_measured_token_density() -> None:
    """A 22k-token Hard gap must not use the old 4-char/token estimate."""
    scenario = _RUNNER.LiveScenario(
        args=SimpleNamespace(min_payload_chars=1800),
        run=None,
        client=None,
        conversation_id="conv_test",
        marker="marker",
    )
    scenario.records = [
        {
            "submitted_char_count": 241,
            "preflight": {"tokens_before": 1725},
        },
        {
            "submitted_char_count": 105039,
            "preflight": {"tokens_before": 68727},
        },
        {
            "submitted_char_count": 105039,
            "preflight": {"tokens_before": 135729},
        },
    ]

    density = scenario._observed_token_density()

    assert density is not None
    assert round(density, 3) == round(67002 / 105039, 3)
    # 157,500 - 135,729 = 21,771 observed tokens -> ~34k chars, not
    # the old fixed-heuristic 87,084 chars that overshot Absolute.
    assert 33000 < 21771 / density < 35000


def test_stage_limit_is_recorded_without_preventing_later_live_evidence(tmp_path: Path) -> None:
    scenario = SimpleNamespace(
        records=[{"label": "hard-compact-3", "preflight": {"waterline": "hard_compact"}}],
        _checkpoint=lambda: "checkpoint.json",
    )
    run = _RUNNER.LctRun(lct="CTX-LONG-01", repo_root=tmp_path, execution_path="test")

    result = _RUNNER._attempt_stage(
        run=run,
        scenario=scenario,
        name="Hard compaction reachability",
        component="Context Engine",
        expected="Hard/COMPACT",
        operation=lambda: (_ for _ in ()).throw(_RUNNER.ScenarioStop("provider unavailable")),
    )

    assert result is None
    assert run.steps[0].status == "FAIL"
    later = run.step("independent audit", component="Audit API", expected="reachable")
    assert run.check(later, True, actual="completed") is True


def test_hard_diagnostic_checkpoint_keeps_only_safe_replay_data(tmp_path: Path, monkeypatch) -> None:
    checkpoint = tmp_path / "CTX-LONG-01-hard-diagnostic-checkpoint.json"
    monkeypatch.setattr(_RUNNER, "_hard_checkpoint_state_path", lambda: checkpoint)

    _RUNNER._write_hard_checkpoint(
        conversation_id="conv_checkpoint",
        turn_count=71,
        records=[{"label": "hard-compact-3", "preflight": {"action": "prune"}}],
        hard_payload_chars=42000,
    )

    saved = _RUNNER._read_hard_checkpoint()

    assert saved is not None
    assert saved["conversation_id"] == "conv_checkpoint"
    assert saved["turn_count"] == 71
    assert saved["hard_payload_chars"] == 42000
    assert "password" not in checkpoint.read_text(encoding="utf-8").lower()


def test_fresh_hard_probe_requires_real_chat_and_completed_preflight_audit() -> None:
    record = {"route": "chat_reply", "snapshot_found": True}
    completed = [{"trigger_type": "preflight", "status": "completed"}]

    assert _RUNNER._fresh_hard_probe_passed(record, completed)
    assert not _RUNNER._fresh_hard_probe_passed(
        {"route": "clarify", "snapshot_found": False}, completed
    )
    assert not _RUNNER._fresh_hard_probe_passed(record, [])


def test_project_memory_runner_creates_real_docx_and_checks_fact_atoms(tmp_path: Path) -> None:
    docs = _PROJECT_RUNNER._make_fixture_docs(tmp_path, "SELFTEST")

    assert len(docs) == 3
    assert {item["role"] for item in docs} == {"requirement", "api_spec", "historical_test"}
    assert all(item["path"].suffix == ".docx" and item["path"].read_bytes().startswith(b"PK") for item in docs)

    api_answer = _PROJECT_RUNNER._atom_result(
        "requestId 的幂等期为 24 小时；重复请求返回 RF409。",
        (("requestId",), ("24小时",), ("RF409",)),
    )
    fresh_conversation_answer = _PROJECT_RUNNER._atom_result(
        "当前项目资料没有提供此前会话的验收负责人，因此我无法确定。",
        (("无法", "不能", "未提供", "不清楚", "未知", "无法确定"),),
        ("王晨",),
    )

    assert api_answer["passed"]
    assert fresh_conversation_answer["passed"]


def test_project_memory_runner_rejects_cross_conversation_decision_leak() -> None:
    leaked = _PROJECT_RUNNER._atom_result(
        "此前已经确定验收负责人是王晨。",
        (("无法", "不能", "未提供", "不清楚", "未知", "无法确定"),),
        ("王晨",),
    )

    assert not leaked["passed"]
    assert "王晨" in leaked["forbidden_hits"]


def test_project_memory_runner_does_not_treat_missing_uploads_as_ready_indexes() -> None:
    ready, statuses = _PROJECT_RUNNER._wait_for_indexing(
        client=None,
        file_ids=[],
        wait_seconds=3,
    )

    assert not ready
    assert statuses == []


def test_project_memory_runner_accepts_the_real_project_public_id_prefix() -> None:
    """The production Project API issues ``prj_`` IDs, not ``project_`` IDs."""
    assert _PROJECT_RUNNER._is_project_public_id("prj_9c6938b0")
    assert not _PROJECT_RUNNER._is_project_public_id("conv_daccc929")


def test_project_memory_runner_uses_a_scope_summary_not_a_test_case_request() -> None:
    delayed_p0_prompt = next(
        prompt
        for label, prompt, _ in _PROJECT_RUNNER.DELAYED_PROBES
        if label == "delayed-p0-matrix"
    )

    assert "测试用例" not in delayed_p0_prompt
    assert "P0" in delayed_p0_prompt
    assert "范围" in delayed_p0_prompt


def test_project_memory_runner_soft_deletes_before_permanent_file_delete() -> None:
    class Client:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def request(self, method: str, path: str):
            assert method == "DELETE"
            self.paths.append(path)
            return {"data": {"ok": True}}

    client = Client()

    outcomes = _PROJECT_RUNNER._delete_records(
        client,
        project_id=None,
        conversation_ids=[],
        file_ids=["file_owned_by_runner"],
    )

    assert outcomes == [{"kind": "library_file", "id": "file_owned_by_runner", "deleted": True}]
    assert client.paths == [
        "/library/items/file_owned_by_runner",
        "/library/items/file_owned_by_runner/permanent",
    ]


def test_context_pressure_runner_builds_varied_real_docx_corpus(tmp_path: Path) -> None:
    docs = _PRESSURE_RUNNER._make_fixture_docs(tmp_path, "SELFTEST")

    assert len(docs) == 10
    assert len({item["label"] for item in docs}) == 10
    assert {"requirement", "api_spec", "design", "technical_spec"}.issubset(
        {item["role"] for item in docs}
    )
    assert all(
        item["path"].suffix == ".docx"
        and item["path"].read_bytes().startswith(b"PK")
        for item in docs
    )


def test_context_pressure_runner_uses_meaningful_unique_work_packets() -> None:
    first_spec, first = _PRESSURE_RUNNER._work_packet(
        ordinal=1, marker="SELFTEST", max_prompt_chars=2000
    )
    second_spec, second = _PRESSURE_RUNNER._work_packet(
        ordinal=2, marker="SELFTEST", max_prompt_chars=2000
    )

    assert len(_PRESSURE_RUNNER.REALISTIC_TURNS) == 30
    assert 100 <= len(first) <= 2000
    assert 100 <= len(second) <= 2000
    assert "项目资料" in first and "订单状态机" in second
    assert "CTX-PROJECT-50:SELFTEST:T01" in first
    assert "CTX-PROJECT-50:SELFTEST:T02" in second
    assert first != second
    assert first_spec.phase == "A-understand"
    assert second_spec.phase == "A-understand"
    assert "target_chars" not in _PRESSURE_RUNNER._work_packet.__doc__


def test_context_pressure_wrapper_targets_backend_venv_and_exact_runner_cli() -> None:
    script = _PRESSURE_WRAPPER_PATH.read_text(encoding="utf-8")

    assert "Split-Path -Parent $PSScriptRoot" in script
    assert "context_scenarios\\project_context_pressure_50.py" in script
    assert "[string]$Username = $env:PHASE2_E2E_USERNAME" in script
    assert "if (-not $env:PHASE2_E2E_PASSWORD)" in script
    assert "if ($addedUsername)" in script
    assert "--max-prompt-chars" in script
    assert "--task-wait-seconds" in script
    assert "--cleanup-on-success" not in script


def test_context_pressure_runner_stays_below_the_60_percent_retention_transition() -> None:
    parser = _PRESSURE_RUNNER._build_parser()
    args = parser.parse_args([])

    assert args.prepare_percent == 40.0
    assert args.target_percent == 45.0
    assert args.max_prompt_chars == 30000
    assert args.max_turns == 30
    assert args.task_wait_seconds == 900
    assert _PRESSURE_RUNNER.SCENARIO == "CTX-PROJECT-50"
    assert _PRESSURE_RUNNER.CONVERSATION_COMPACTION_WATERLINE_PERCENT == 60.0


def test_compaction_lifecycle_uses_real_short_turns_and_paused_exports() -> None:
    prompt = _LIFECYCLE_RUNNER._turn_prompt(ordinal=21, marker="SELFTEST")
    parser = _LIFECYCLE_RUNNER._parser()
    args = parser.parse_args([])

    assert _LIFECYCLE_RUNNER.PROACTIVE_TOKENS == 120_000
    assert _LIFECYCLE_RUNNER.EXPORTER.name == "export_conversation_context_working_set.py"
    assert 80 < len(prompt) < 300
    assert "不要创建任务或文件" in prompt
    assert args.max_turns_per_round == 600
    assert "Hard/Absolute" in _LIFECYCLE_RUNNER.__doc__


def test_compaction_lifecycle_wrapper_requires_environment_credentials_and_resume_review() -> None:
    script = _LIFECYCLE_WRAPPER_PATH.read_text(encoding="utf-8")

    assert "conversation_compaction_lifecycle.py" in script
    assert "PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD must be set" in script
    assert "-AnalysisFile requires -Resume" in script


def test_context_pressure_runner_reads_the_previous_75_percent_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    current = tmp_path / _PRESSURE_RUNNER.STATE_NAME
    legacy = tmp_path / _PRESSURE_RUNNER.LEGACY_STATE_NAME
    legacy.write_text(
        json.dumps({
            "scenario": "CTX-PROJECT-75",
            "project_id": "prj_old",
            "conversation_id": "conv_old",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(_PRESSURE_RUNNER, "_state_paths", lambda: (current, legacy))

    state = _PRESSURE_RUNNER._read_state()

    assert state is not None
    assert state["project_id"] == "prj_old"


def test_context_pressure_runner_separates_raw_pressure_from_visible_usage() -> None:
    metrics = _PRESSURE_RUNNER._pressure_metrics({
        "model": {"context_window_tokens": 200000},
        "usage": {"used_tokens": 126000, "percent": 63.0},
        "preflight": {
            "tokens_before": 164000,
            "tokens_after": 126000,
            "waterline": "hard_compact",
            "action": "prune",
        },
        "breakdown": {category: 1 for category in _PRESSURE_RUNNER.UI_CATEGORIES},
    })

    assert metrics["raw_pressure_percent"] == 82.0
    assert metrics["visible_percent"] == 63.0
    assert metrics["tokens_after"] == 126000
    assert _PRESSURE_RUNNER._all_categories_active(metrics)


def test_context_pressure_runner_requires_every_usage_category() -> None:
    breakdown = {category: 1 for category in _PRESSURE_RUNNER.UI_CATEGORIES}
    assert _PRESSURE_RUNNER._all_categories_active({"breakdown": breakdown})

    breakdown["task_context"] = 0
    assert not _PRESSURE_RUNNER._all_categories_active({"breakdown": breakdown})


def test_context_pressure_runner_creates_task_context_via_message_api() -> None:
    calls: list[tuple[str, str, dict]] = []

    class _Client:
        def request(self, method: str, path: str, payload: dict):
            calls.append((method, path, payload))
            return {"route": "agent_task", "task_id": "task_123"}

    task_id = _PRESSURE_RUNNER._create_real_task_context(
        _Client(),
        conversation_id="conv_123",
        requirement_file_id="file_requirement",
    )

    assert task_id == "task_123"
    assert calls == [
        (
            "POST",
            "/conversations/conv_123/messages",
            {
                "content": calls[0][2]["content"],
                "attached_file_ids": ["file_requirement"],
                "knowledge_mode_snapshot": "AUTO",
            },
        )
    ]
    assert "测试方案" in calls[0][2]["content"]


def test_context_pressure_runner_confirms_agent_recommendations_then_waits_for_completion() -> None:
    class _Client:
        def __init__(self) -> None:
            self.statuses = iter([
                {"status": "running"},
                {"status": "waiting_user_confirm"},
                {"status": "running"},
                {
                    "status": "completed",
                    "artifact": {
                        "artifact_id": "art_123",
                        "artifact_type": "test_plan_word",
                        "status": "available",
                        "file_ext": "docx",
                    },
                },
            ])
            self.confirm_payloads: list[dict] = []

        def request(self, method: str, path: str, payload=None):
            if method == "GET" and path == "/agent/tasks/task_123":
                return next(self.statuses)
            if method == "GET" and path.endswith("/pending-confirmation"):
                return {
                    "confirmation_id": "confirmation_123",
                    "sections": [
                        {"section_id": "scope", "suggested_action": "ai_generate"},
                        {"section_id": "revision", "suggested_action": "keep_template"},
                    ],
                }
            if method == "POST" and path.endswith("/confirm"):
                self.confirm_payloads.append(payload)
                return {"task_id": "task_123", "status": "running"}
            raise AssertionError(f"unexpected request: {method} {path}")

    client = _Client()
    completed = _PRESSURE_RUNNER._wait_for_task_completion(
        client, task_id="task_123", wait_seconds=30
    )

    assert completed["final_status"] == "completed"
    assert completed["confirmation_id"] == "confirmation_123"
    assert completed["confirmed_sections"] == [
        {"section_id": "scope", "action": "ai_generate"},
        {"section_id": "revision", "action": "keep_template"},
    ]
    assert client.confirm_payloads == [{"sections": completed["confirmed_sections"]}]
    assert completed["artifact"]["artifact_id"] == "art_123"
    assert completed["transitions"] == [
        "running", "waiting_user_confirm", "section_confirmation_submitted", "running", "completed"
    ]


def test_context_pressure_runner_handles_preparation_clarification_before_sections() -> None:
    class _Client:
        def __init__(self) -> None:
            self.statuses = iter([
                {"status": "running"},
                {"status": "waiting_user_confirm"},
                {"status": "running"},
                {"status": "waiting_user_confirm"},
                {"status": "running"},
                {
                    "status": "completed",
                    "artifact": {
                        "artifact_id": "art_123",
                        "artifact_type": "test_plan_word",
                        "status": "available",
                        "file_ext": "docx",
                    },
                },
            ])
            self.pending = iter([
                {
                    "confirmation_id": "clarification_123",
                    "confirmation_type": "preparation_clarification",
                    "cards": [
                        {"id": "release_scope", "allow_conservative_scope": True},
                        {"id": "acceptance_rule", "allow_conservative_scope": False},
                    ],
                },
                {
                    "confirmation_id": "sections_123",
                    "confirmation_type": "section_generation_config",
                    "sections": [{"section_id": "scope", "suggested_action": "ai_generate"}],
                },
            ])
            self.clarification_payloads: list[dict] = []
            self.section_payloads: list[dict] = []

        def request(self, method: str, path: str, payload=None):
            if method == "GET" and path == "/agent/tasks/task_123":
                return next(self.statuses)
            if method == "GET" and path.endswith("/pending-confirmation"):
                return next(self.pending)
            if method == "POST" and path.endswith("/preparation-clarification"):
                self.clarification_payloads.append(payload)
                return {"task_id": "task_123", "status": "resuming"}
            if method == "POST" and path.endswith("/confirm"):
                self.section_payloads.append(payload)
                return {"task_id": "task_123", "status": "running"}
            raise AssertionError(f"unexpected request: {method} {path}")

    client = _Client()
    completed = _PRESSURE_RUNNER._wait_for_task_completion(
        client, task_id="task_123", wait_seconds=30
    )

    assert client.clarification_payloads == [{
        "answers": {"acceptance_rule": _PRESSURE_RUNNER._SCENARIO_CLARIFICATION_ANSWER},
        "conservative_gap_ids": ["release_scope"],
    }]
    assert client.section_payloads == [{"sections": [{"section_id": "scope", "action": "ai_generate"}]}]
    assert completed["transitions"] == [
        "running",
        "waiting_user_confirm",
        "preparation_clarification_submitted",
        "running",
        "waiting_user_confirm",
        "section_confirmation_submitted",
        "running",
        "completed",
    ]
    assert [event["confirmation_type"] for event in completed["confirmation_events"]] == [
        "preparation_clarification", "section_generation_config"
    ]
    assert completed["artifact"]["artifact_id"] == "art_123"


def test_context_pressure_runner_rejects_completed_task_without_available_docx_artifact() -> None:
    with pytest.raises(RuntimeError, match="did not expose an available DOCX artifact"):
        _PRESSURE_RUNNER._completed_test_plan_artifact({
            "status": "completed",
            "artifact": {
                "artifact_id": "art_123",
                "artifact_type": "test_plan_word",
                "status": "available",
                "file_ext": "pdf",
            },
        })


def test_context_pressure_runner_rejects_invalid_pending_confirmation_action() -> None:
    with pytest.raises(RuntimeError, match="invalid section recommendation"):
        _PRESSURE_RUNNER._recommended_confirmation_sections({
            "sections": [{"section_id": "scope", "suggested_action": "erase_template"}],
        })


def test_context_pressure_runner_accepts_explicit_lexical_fallback(monkeypatch) -> None:
    rows = [{
        "source_public_id": "file_1",
        "status": "indexed",
        "lexical_index_status": "ready",
        "vector_index_status": "failed",
    }]
    monkeypatch.setattr(_PRESSURE_RUNNER, "_index_statuses", lambda *_args: rows)

    ready, statuses = _PRESSURE_RUNNER._wait_for_indexing(None, ["file_1"], 0)

    assert ready
    assert statuses == rows


def test_context_pressure_runner_retains_created_specimen_on_runtime_failure(
    tmp_path: Path, monkeypatch
) -> None:
    saved_states: list[dict] = []

    class _Client:
        def __init__(self, *_args, **_kwargs) -> None:
            self.calls: list[dict] = []
            self.upload_count = 0

        def login(self, *_args) -> None:
            return None

        def request(self, method: str, path: str, payload=None):
            if method == "POST" and path == "/projects":
                return {"id": "prj_53df0520"}
            if method == "POST" and path.endswith("/conversations"):
                return {"id": "conv_19c959be"}
            if method == "PUT" and path.endswith("/instructions"):
                return {"ok": True}
            if method == "POST" and path.endswith("/confirm-type"):
                return {"file_id": "file_conversation_requirement"}
            raise AssertionError(f"unexpected request: {method} {path}")

        def upload(self, *_args, **_kwargs):
            self.upload_count += 1
            path = _args[0]
            if path == "/files/upload":
                return {"id": "file_conversation_requirement"}
            return {"fileId": f"file_{self.upload_count}"}

    usage = {
        "model": {"context_window_tokens": 200_000},
        "usage": {"used_tokens": 20_000, "percent": 10.0},
        "preflight": {
            "tokens_before": 20_000,
            "tokens_after": 20_000,
            "waterline": "target",
            "action": "pass",
        },
        "breakdown": {category: 1 for category in _PRESSURE_RUNNER.UI_CATEGORIES},
    }

    monkeypatch.setenv("PHASE2_E2E_USERNAME", "scenario-user")
    monkeypatch.setenv("PHASE2_E2E_PASSWORD", "scenario-password")
    monkeypatch.setattr(_PRESSURE_RUNNER, "MultipartApiClient", _Client)
    monkeypatch.setattr(_PRESSURE_RUNNER, "_read_state", lambda: None)
    requirement_path = tmp_path / "requirement.docx"
    requirement_path.write_bytes(b"requirement")
    monkeypatch.setattr(
        _PRESSURE_RUNNER,
        "_make_fixture_docs",
        lambda *_args: [
            {"label": "业务需求基线", "filename": "requirement.docx", "role": "requirement", "path": requirement_path},
            {"label": "UAT 验收计划", "filename": "uat.docx", "role": "historical_test", "path": requirement_path},
        ],
    )
    monkeypatch.setattr(_PRESSURE_RUNNER, "_create_active_memories", lambda *_args: ["mem_1"])
    monkeypatch.setattr(_PRESSURE_RUNNER, "_create_real_task_context", lambda *_args, **_kwargs: "task_1")
    monkeypatch.setattr(
        _PRESSURE_RUNNER,
        "_wait_for_task_completion",
        lambda *_args, **_kwargs: {"final_status": "completed"},
    )
    monkeypatch.setattr(_PRESSURE_RUNNER, "_wait_for_indexing", lambda *_args: (True, []))
    monkeypatch.setattr(_PRESSURE_RUNNER, "_idle_usage", lambda *_args: usage)
    monkeypatch.setattr(_PRESSURE_RUNNER, "_usage", lambda *_args, **_kwargs: usage)
    monkeypatch.setattr(
        _PRESSURE_RUNNER,
        "_send_chat",
        lambda *_args: {"route": "clarify"},
    )
    monkeypatch.setattr(_PRESSURE_RUNNER, "_write_state", saved_states.append)
    monkeypatch.setattr(
        _PRESSURE_RUNNER,
        "_delete_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("created specimens must not be deleted on failure")
        ),
    )
    monkeypatch.setattr(
        _PRESSURE_RUNNER,
        "_delete_memories",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("created specimen memories must not be deleted on failure")
        ),
    )
    args = SimpleNamespace(
        base_url="http://test/api",
        request_timeout_seconds=1,
        index_wait_seconds=0,
        task_wait_seconds=30,
        prepare_percent=50.0,
        target_percent=75.0,
        max_prompt_chars=2_000,
        max_turns=5,
    )
    run = _PRESSURE_RUNNER.LctRun(
        lct="CTX-PROJECT-50",
        repo_root=tmp_path,
        execution_path="test",
    )

    with pytest.raises(RuntimeError, match="turn 1 did not complete as a synchronous chat reply"):
        _PRESSURE_RUNNER._run(args, run)

    assert len(saved_states) == 1
    assert saved_states[0]["project_id"] == "prj_53df0520"
    assert saved_states[0]["conversation_id"] == "conv_19c959be"
    assert saved_states[0]["task_id"] == "task_1"
    assert saved_states[0]["failure"]["type"] == "RuntimeError"
    assert saved_states[0]["measurements"]
