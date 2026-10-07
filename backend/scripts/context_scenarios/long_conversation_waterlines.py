"""Real API + real model scenario for automatic chat-context waterlines.

This is intentionally a black-box scenario: it never imports application
business code, never writes the database directly, and never invokes the
manual compaction endpoint.  Every turn goes through the normal authenticated
chat API, and every assertion comes from owner-scoped audit APIs plus the
visible assistant reply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = SCRIPTS_ROOT.parent
for path in (SCRIPTS_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from phase2_lct_runtime import LctRun, cli_failure, repository_root
from phase2_live_api_e2e import ApiClient, ApiFailure, _data


class ScenarioStop(RuntimeError):
    """A deterministic scenario limit or required waterline was not reached."""


_SCENARIO_TITLE_PREFIX = "Context waterline scenario "
_LATEST_PASSED_STATE_NAME = "CTX-LONG-01-latest-passed-conversation.json"
_HARD_CHECKPOINT_STATE_NAME = "CTX-LONG-01-hard-diagnostic-checkpoint.json"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _reply_text(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    reply = response.get("agent_reply")
    return str(reply.get("content") or "") if isinstance(reply, dict) else ""


def _safe_intent_routing_diagnostics(response: Any) -> dict[str, Any]:
    """Extract size-only routing diagnostics from the persisted assistant payload."""
    if not isinstance(response, dict):
        return {}
    reply = response.get("agent_reply")
    payload = reply.get("payload") if isinstance(reply, dict) else None
    if not isinstance(payload, dict):
        return {}
    keys = (
        "intent_routing_input_chars",
        "intent_routing_original_message_chars",
        "intent_routing_current_message_bounded",
    )
    return {key: payload[key] for key in keys if key in payload}


def _safe_intent_context_diagnostics(response: Any) -> dict[str, Any]:
    """Extract content-free Context Engine evidence from intent routing."""
    if not isinstance(response, dict):
        return {}
    reply = response.get("agent_reply")
    payload = reply.get("payload") if isinstance(reply, dict) else None
    value = payload.get("intent_context_engine") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        return {}
    allowed = (
        "path", "outcome", "snapshot_public_id", "exception_type", "error_code",
        "stage", "retryable", "reason", "compaction_attempted",
        "compaction_compactor_available", "compaction_runtime_context_available",
        "compaction_phase", "compaction_exception_type", "compaction_exception_code",
    )
    return {key: value[key] for key in allowed if key in value}


def _safe_chat_context_diagnostics(response: Any) -> dict[str, Any]:
    """Extract the content-free Context Engine outcome for this chat turn."""
    if not isinstance(response, dict):
        return {}
    reply = response.get("agent_reply")
    payload = reply.get("payload") if isinstance(reply, dict) else None
    value = payload.get("chat_context_engine") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        return {}
    allowed = (
        "path",
        "outcome",
        "snapshot_public_id",
        "exception_type",
        "error_code",
        "stage",
        "retryable",
        "reason",
        "compaction_attempted",
        "compaction_compactor_available",
        "compaction_runtime_context_available",
        "compaction_phase",
        "compaction_exception_type",
        "compaction_exception_code",
    )
    return {key: value[key] for key in allowed if key in value}


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _make_chat_note(*, ordinal: int, char_budget: int, marker: str | None = None) -> str:
    """Create ordinary chat text without storing it in result evidence."""
    prefix = (
        "我们正在进行普通聊天。请把下列日记作为后续聊天的背景材料，"
        f"并且只回答“已记录第{ordinal}篇日记”。【日记开始】"
    )
    if marker:
        prefix += f"日记中特别写下的长期记忆代号是 {marker}，以后讨论时需要记住它。"
    unit = (
        "今天我记录了旅途中观察到的天气、街道声音和同行者的想法；"
        "这些细节只是我们普通聊天时可继续讨论的背景，不需要创建任务或文档。"
    )
    repeats = max(1, math.ceil(max(0, char_budget - len(prefix)) / len(unit)))
    return prefix + unit * repeats + "【日记结束】"


def _create_conversation(client: ApiClient, title: str) -> str:
    data = _data(client.request("POST", "/conversations", {"title": title}))
    conversation_id = data.get("id") if isinstance(data, dict) else None
    if not conversation_id:
        raise RuntimeError("create-conversation response contained no id")
    return str(conversation_id)


def _snapshots(client: ApiClient, conversation_id: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"conversation_id": conversation_id, "limit": 20})
    data = _data(client.request("GET", f"/context/audit/snapshots?{query}"))
    items = data.get("items", []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict) and item.get("call_site") == "chat.reply"]


def _latest_chat_snapshot(client: ApiClient, conversation_id: str) -> dict[str, Any] | None:
    items = _snapshots(client, conversation_id)
    return items[0] if items else None


def _telemetry(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("preflight")
    return dict(value) if isinstance(value, dict) and value else None


def _waterline_rank(value: str | None) -> int:
    return {"target": 0, "soft": 1, "hard_compact": 2, "absolute": 3}.get(value or "", -1)


def _safe_snapshot_evidence(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {"snapshot_found": False}
    return {
        "snapshot_found": True,
        "snapshot_public_id": snapshot.get("public_id"),
        "status": snapshot.get("status"),
        "budget": snapshot.get("budget"),
        "token_usage": snapshot.get("token_usage"),
        "preflight": snapshot.get("preflight"),
    }


def _safe_api_failure(exc: ApiFailure) -> dict[str, Any]:
    """Keep enough server-side diagnostics for triage without recording prompts.

    The HTTP client deliberately never records request bodies.  A server error
    can, however, otherwise leave the scenario with only an opaque status code.
    We retain a short, credential-redacted error summary supplied by the API.
    """
    body = exc.body if isinstance(exc.body, dict) else {}
    detail = next(
        (
            str(body[key])
            for key in ("detail", "message", "error", "code")
            if body.get(key) is not None
        ),
        "",
    )
    detail = re.sub(r"(?i)(password|token|authorization)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", detail)
    return {
        "method": exc.method,
        "path": exc.path,
        "status": exc.status,
        "response_keys": sorted(str(key) for key in body.keys()),
        "response_detail": detail[:500] or None,
    }


def _safe_transport_failure(
    exc: TimeoutError | OSError,
    *,
    method: str,
    path: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Describe a request that never reached an HTTP response safely.

    A POST must not be retried automatically: after a client timeout we do not
    know whether the server finished persisting the message.  Record only the
    transport boundary and timeout configuration, never its prompt body.
    """
    return {
        "method": method,
        "path": path,
        "status": None,
        "failure_type": type(exc).__name__,
        "timeout_seconds": timeout_seconds,
        "response_keys": [],
        "response_detail": None,
    }


def _latest_passed_state_path() -> Path:
    return repository_root() / "test-results" / "phase2-resource-pack" / _LATEST_PASSED_STATE_NAME


def _read_latest_passed_conversation_id() -> str | None:
    """Return only an ID written by this runner; invalid local state is ignored."""
    try:
        payload = json.loads(_latest_passed_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("conversation_id") if isinstance(payload, dict) else None
    return str(value) if isinstance(value, str) and value.startswith("conv_") else None


def _write_latest_passed_conversation_id(conversation_id: str) -> None:
    path = _latest_passed_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scenario": "CTX-LONG-01",
                "conversation_id": conversation_id,
                "retained_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _hard_checkpoint_state_path() -> Path:
    return repository_root() / "test-results" / "phase2-resource-pack" / _HARD_CHECKPOINT_STATE_NAME


def _write_hard_checkpoint(*, conversation_id: str, turn_count: int, records: list[dict[str, Any]], hard_payload_chars: int) -> None:
    """Persist only script-generated IDs/numbers and safe per-turn evidence."""
    path = _hard_checkpoint_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scenario": "CTX-LONG-01",
                "phase": "after_hard_diagnostic",
                "conversation_id": conversation_id,
                "turn_count": turn_count,
                "hard_payload_chars": hard_payload_chars,
                "records": records,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_hard_checkpoint() -> dict[str, Any] | None:
    try:
        value = json.loads(_hard_checkpoint_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    conversation_id = value.get("conversation_id")
    records = value.get("records")
    payload_chars = value.get("hard_payload_chars")
    turn_count = value.get("turn_count")
    if (
        value.get("scenario") != "CTX-LONG-01"
        or value.get("phase") != "after_hard_diagnostic"
        or not isinstance(conversation_id, str)
        or not conversation_id.startswith("conv_")
        or not isinstance(records, list)
        or not isinstance(payload_chars, int)
        or payload_chars <= 0
        or not isinstance(turn_count, int)
        or turn_count < 1
    ):
        return None
    return value


def _clear_hard_checkpoint() -> None:
    try:
        _hard_checkpoint_state_path().unlink()
    except FileNotFoundError:
        pass


def _is_owned_scenario_conversation(client: ApiClient, conversation_id: str) -> bool:
    """Prevent a local state file from broadening deletion beyond this scenario."""
    try:
        detail = _data(client.request("GET", f"/conversations/{urllib.parse.quote(conversation_id)}"))
    except (ApiFailure, OSError, TimeoutError):
        return False
    return isinstance(detail, dict) and str(detail.get("title") or "").startswith(_SCENARIO_TITLE_PREFIX)


class LiveScenario:
    def __init__(self, *, args: argparse.Namespace, run: LctRun, client: ApiClient, conversation_id: str, marker: str) -> None:
        self.args = args
        self.run = run
        self.client = client
        self.conversation_id = conversation_id
        self.marker = marker
        self.turn_count = 0
        self.records: list[dict[str, Any]] = []

    def _checkpoint(self, *, failure: dict[str, Any] | None = None) -> str:
        """Persist progress after every live turn so a mid-run failure is useful."""
        return self.run.write_evidence(
            "live-progress.json",
            {
                "scenario": "CTX-LONG-01",
                "conversation_id": self.conversation_id,
                "turn_count": self.turn_count,
                "marker_sha256_16": _short_hash(self.marker),
                "records": self.records,
                "api_calls": self.client.calls,
                "last_api_failure": failure,
            },
        )

    def send(self, *, label: str, char_budget: int, include_marker: bool = False, recall_query: bool = False) -> dict[str, Any]:
        if self.turn_count >= self.args.max_turns:
            raise ScenarioStop(f"max turn limit reached before {label}: {self.args.max_turns}")
        self.turn_count += 1
        content = (
            "请逐字回复我们在早期约定的长期记忆代号；不要解释。"
            if recall_query
            else _make_chat_note(
                ordinal=self.turn_count,
                char_budget=char_budget,
                marker=self.marker if include_marker else None,
            )
        )
        previous_snapshot_public_id = (
            self.records[-1].get("snapshot_public_id") if self.records else None
        )
        path = f"/conversations/{urllib.parse.quote(self.conversation_id)}/messages"
        try:
            response = _data(
                self.client.request(
                    "POST",
                    path,
                    {"content": content, "attached_file_ids": [], "knowledge_mode_snapshot": "AUTO"},
                )
            )
        except (ApiFailure, TimeoutError, OSError) as exc:
            diagnostic = (
                _safe_api_failure(exc)
                if isinstance(exc, ApiFailure)
                else _safe_transport_failure(
                    exc,
                    method="POST",
                    path=path,
                    timeout_seconds=self.args.request_timeout_seconds,
                )
            )
            if not isinstance(exc, ApiFailure):
                self.client.calls.append(
                    {
                        "method": "POST",
                        "path": path,
                        "status": None,
                        "request_id": None,
                        "failure_type": type(exc).__name__,
                    }
                )
            progress_path = self._checkpoint(failure=diagnostic)
            step = self.run.step(
                "normal-chat API request",
                component="Live normal chat API",
                expected="each generated turn returns a successful normal-chat response",
            )
            self.run.observe(
                step,
                False,
                actual=json.dumps(diagnostic, ensure_ascii=False),
                evidence={"progress_evidence": progress_path, "failed_turn": self.turn_count, "label": label},
            )
            raise AssertionError("unreachable")
        # A clarify/unsupported response never entered chat.reply.  Do not
        # report its predecessor's snapshot as if it belonged to this request.
        snapshot = (
            _latest_chat_snapshot(self.client, self.conversation_id)
            if isinstance(response, dict) and response.get("route") == "chat_reply"
            else None
        )
        reply = _reply_text(response)
        record = {
            "label": label,
            "turn": self.turn_count,
            "submitted_char_count": len(content),
            "route": response.get("route") if isinstance(response, dict) else None,
            "intent": response.get("intent") if isinstance(response, dict) else None,
            "requires_sse": response.get("requires_sse") if isinstance(response, dict) else None,
            "reply_length": len(reply),
            "reply_sha256_16": _short_hash(reply),
            "reply_contains_marker": self.marker in reply,
            "intent_routing_diagnostics": _safe_intent_routing_diagnostics(response),
            "intent_context_diagnostics": _safe_intent_context_diagnostics(response),
            "chat_context_diagnostics": _safe_chat_context_diagnostics(response),
            **_safe_snapshot_evidence(snapshot),
        }
        self.records.append(record)
        progress_path = self._checkpoint()
        if record["route"] != "chat_reply":
            step = self.run.step(
                "normal-chat route guard",
                component="Intent routing + normal chat API",
                expected="each scenario message is classified as chat_reply before Context Engine assertions",
            )
            self.run.observe(
                step,
                False,
                actual=json.dumps(
                    {
                        "label": label,
                        "route": record["route"],
                        "intent": record["intent"],
                        "intent_context_diagnostics": record["intent_context_diagnostics"],
                    },
                    ensure_ascii=False,
                ),
                evidence={"progress_evidence": progress_path, "turn": self.turn_count},
            )
        context_diagnostics = record["chat_context_diagnostics"]
        if (
            isinstance(context_diagnostics, dict)
            and context_diagnostics.get("outcome") == "bridge_exception"
        ):
            step = self.run.step(
                "chat Context Engine composition guard",
                component="Context Engine preflight",
                expected="chat context composition completes without a blocking Context Engine error",
            )
            self.run.observe(
                step,
                False,
                actual=json.dumps(
                    {"label": label, "context_diagnostics": context_diagnostics},
                    ensure_ascii=False,
                ),
                evidence={"progress_evidence": progress_path, "turn": self.turn_count},
            )
        if previous_snapshot_public_id and record.get("snapshot_public_id") == previous_snapshot_public_id:
            step = self.run.step(
                "fresh chat snapshot guard",
                component="Context Audit API",
                expected="each chat_reply creates a new chat.reply snapshot for its own request",
            )
            self.run.observe(
                step,
                False,
                actual=json.dumps(
                    {"label": label, "snapshot_public_id": record.get("snapshot_public_id")},
                    ensure_ascii=False,
                ),
                evidence={"progress_evidence": progress_path, "turn": self.turn_count},
            )
        return record

    def _observed_token_density(self) -> float | None:
        """Return median preflight-token growth per submitted character.

        The live tokenizer, prompt framing and selected history determine this
        ratio.  A character-to-token constant is unsafe: in the failing run,
        the old 4-char heuristic turned a 22k-token gap into an 87k-character
        message and jumped directly past Absolute.
        """
        densities: list[float] = []
        for index, record in enumerate(self.records):
            if index == 0:
                continue
            current = record.get("preflight")
            previous = self.records[index - 1].get("preflight")
            chars = int(record.get("submitted_char_count") or 0)
            if not isinstance(current, dict) or not isinstance(previous, dict):
                continue
            growth = int(current.get("tokens_before") or 0) - int(
                previous.get("tokens_before") or 0
            )
            if chars >= self.args.min_payload_chars and growth > 0:
                densities.append(growth / chars)
        return sorted(densities)[len(densities) // 2] if densities else None

    def drive(self, *, name: str, expected_waterline: str, expected_action: str, marker_required: bool = False) -> dict[str, Any]:
        last = self.records[-1] if self.records else {}
        for attempt in range(1, self.args.max_stage_attempts + 1):
            preflight = last.get("preflight") if isinstance(last, dict) else None
            if isinstance(preflight, dict):
                current = int(preflight.get("tokens_before") or 0)
                soft = int(preflight.get("soft_threshold") or 0)
                hard = int(preflight.get("hard_threshold") or 0)
                absolute = int(preflight.get("absolute_threshold") or 0)
                desired = {
                    "soft": soft + max(160, (hard - soft) // 4),
                    "hard_compact": hard + max(160, (absolute - hard) // 4),
                    "absolute": absolute + max(160, absolute // 40),
                }[expected_waterline]
                # The current message becomes a conversation item on the next
                # turn.  Once this run has a measured token density, use it
                # instead of a fixed chars/token guess so adjacent waterlines
                # are approached without jumping past Absolute.
                token_gap = max(1, desired - current)
                density = self._observed_token_density()
                char_budget = (
                    math.ceil(token_gap / density)
                    if density is not None
                    else token_gap * 4
                )
                char_budget = max(self.args.min_payload_chars, char_budget)
            else:
                char_budget = self.args.min_payload_chars
            char_budget = min(self.args.max_payload_chars, int(char_budget))
            last = self.send(
                label=f"{name}-{attempt}",
                char_budget=char_budget,
                include_marker=marker_required,
            )
            preflight = last.get("preflight")
            if not isinstance(preflight, dict):
                raise ScenarioStop("preflight telemetry disappeared during live chat")
            waterline = str(preflight.get("waterline") or "")
            action = str(preflight.get("action") or "")
            if waterline == expected_waterline and action == expected_action:
                return last
            if _waterline_rank(waterline) > _waterline_rank(expected_waterline):
                raise ScenarioStop(
                    f"overshot {expected_waterline}: observed waterline={waterline}, action={action}"
                )
        raise ScenarioStop(
            f"did not reach {expected_waterline}/{expected_action} in {self.args.max_stage_attempts} adaptive turns"
        )

    def calibrated_payload_chars(self, *, target_tokens: int, retained_message_count: int = 3) -> int:
        """Derive a real-message size from this run's observed token growth.

        The production tokenizer and provider prompt framing decide the actual
        token count.  We therefore calibrate from the early, still-unpruned
        Soft-stage turns instead of pretending that a fixed character count is
        a token budget.
        """
        density = self._observed_token_density()
        if density is None:
            raise ScenarioStop("could not calibrate token growth from real Soft-stage messages")
        required_per_message = max(1, math.ceil(target_tokens / max(1, retained_message_count)))
        estimated_chars = math.ceil(required_per_message / density)
        return min(
            self.args.max_payload_chars,
            max(self.args.min_payload_chars, estimated_chars),
        )

    def drive_fixed_payload(
        self,
        *,
        name: str,
        expected_waterline: str,
        expected_action: str,
        char_budget: int,
        attempts: int = 3,
        marker_on_attempt: int | None = None,
    ) -> dict[str, Any]:
        """Accumulate a measured payload shape under the real source window.

        A Hard compact requires the five-item deterministic pruner to be
        unable to get below Hard.  Repeating a 50k message cannot prove that:
        the pruner continuously brings the selected set back below Hard.  This
        method first clears the source window, then sends three calibrated
        independent messages so the post-prune set itself crosses the desired
        threshold.
        """
        for attempt in range(1, attempts + 1):
            record = self.send(
                label=f"{name}-{attempt}",
                char_budget=char_budget,
                include_marker=marker_on_attempt == attempt,
            )
            preflight = record.get("preflight")
            if not isinstance(preflight, dict):
                raise ScenarioStop("preflight telemetry disappeared during fixed-payload live chat")
            waterline = str(preflight.get("waterline") or "")
            action = str(preflight.get("action") or "")
            if waterline == expected_waterline and action == expected_action:
                return record
            if _waterline_rank(waterline) > _waterline_rank(expected_waterline):
                raise ScenarioStop(
                    f"overshot {expected_waterline}: observed waterline={waterline}, action={action}, "
                    f"char_budget={char_budget}"
                )
        raise ScenarioStop(
            f"did not reach {expected_waterline}/{expected_action} after {attempts} calibrated "
            f"messages of {char_budget} characters"
        )


def _compaction_items(client: ApiClient, conversation_id: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"conversation_id": conversation_id, "limit": 50})
    data = _data(client.request("GET", f"/context/audit/compaction?{query}"))
    items = data.get("items", []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _attempt_stage(
    *,
    run: LctRun,
    scenario: LiveScenario,
    name: str,
    component: str,
    expected: str,
    operation,
) -> dict[str, Any] | None:
    """Run a costly live stage without discarding later independent evidence.

    A waterline miss or an adaptive-payload overshoot is an oracle failure,
    not an unsafe API state.  Preserve it and let later stages/audits run on
    the same conversation.  Auth, conversation creation and an uncertain
    POST failure remain terminal in their respective call paths.
    """
    try:
        return operation()
    except ScenarioStop as exc:
        latest = scenario.records[-1] if scenario.records else None
        step = run.step(name, component=component, expected=expected)
        run.observe(
            step,
            False,
            actual=str(exc),
            evidence={
                "latest_turn": latest,
                "progress_evidence": scenario._checkpoint(),
            },
        )
        run.notes.append(f"CONTINUED_AFTER_STAGE_FAILURE: {name}: {exc}")
        return None


def _preflight_or_empty(record: dict[str, Any] | None) -> dict[str, Any]:
    value = record.get("preflight") if isinstance(record, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def _resume_hard_checkpoint(args: argparse.Namespace, run: LctRun, client: ApiClient) -> str:
    """Retry only the real automatic Hard boundary after a code repair.

    This deliberately does not claim full LCT completion: Soft/Absolute/recall
    are not rerun here.  Its purpose is to avoid buying another 50 warmups for
    each Hard-path repair.
    """
    checkpoint = _read_hard_checkpoint()
    if checkpoint is None:
        raise RuntimeError("no valid CTX-LONG-01 Hard diagnostic checkpoint exists")
    conversation_id = str(checkpoint["conversation_id"])
    if any(
        item.get("label") == "hard-compaction-retry"
        for item in checkpoint["records"]
        if isinstance(item, dict)
    ):
        raise RuntimeError(
            "Hard diagnostic checkpoint was already consumed by a retry; "
            "create a fresh checkpoint instead of appending more large messages"
        )
    if not _is_owned_scenario_conversation(client, conversation_id):
        raise RuntimeError("Hard diagnostic checkpoint conversation is unavailable or not owned by this scenario")
    scenario = LiveScenario(
        args=args,
        run=run,
        client=client,
        conversation_id=conversation_id,
        marker="CHECKPOINT_MARKER_NOT_USED",
    )
    scenario.turn_count = int(checkpoint["turn_count"])
    scenario.records = [item for item in checkpoint["records"] if isinstance(item, dict)]
    run.log("CTX-LONG-01: resume retained Hard diagnostic checkpoint without replaying warmups")
    retry = scenario.send(
        label="hard-compaction-retry",
        char_budget=int(checkpoint["hard_payload_chars"]),
    )
    preflight = _preflight_or_empty(retry)
    step = run.step(
        "Hard waterline repaired compaction retry",
        component="Context Engine + configured compression model",
        expected="retained real conversation reaches Hard/COMPACT after repair without replaying warmups",
    )
    passed = (
        preflight.get("waterline") == "hard_compact"
        and preflight.get("action") == "compact"
        and preflight.get("compression_provider_call_count") == 1
        and retry.get("route") == "chat_reply"
    )
    run.observe(
        step,
        passed,
        actual=json.dumps(
            {"preflight": preflight, "chat_context_diagnostics": retry.get("chat_context_diagnostics")},
            ensure_ascii=False,
        ),
        evidence={"retry_turn": retry, "checkpoint": str(_hard_checkpoint_state_path())},
    )
    _write_hard_checkpoint(
        conversation_id=conversation_id,
        turn_count=scenario.turn_count,
        records=scenario.records,
        hard_payload_chars=int(checkpoint["hard_payload_chars"]),
    )
    evidence_path = run.write_evidence(
        "hard-checkpoint-retry.json",
        {
            "scenario": "CTX-LONG-01",
            "mode": "hard_checkpoint_retry",
            "conversation_id": conversation_id,
            "turn_count": scenario.turn_count,
            "retry_turn": retry,
            "api_calls": client.calls,
        },
    )
    for item in run.steps:
        item.evidence.setdefault("scenario_evidence", evidence_path)
    if passed:
        _data(client.request("DELETE", f"/conversations/{urllib.parse.quote(conversation_id)}"))
        _clear_hard_checkpoint()
        return "LCT_PARTIAL_PASS"
    return "TEST_FAIL"


def _completed_preflight_compactions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in items
        if item.get("trigger_type") == "preflight" and item.get("status") == "completed"
    ]


def _fresh_hard_probe_passed(
    record: dict[str, Any],
    completed_compactions: list[dict[str, Any]],
) -> bool:
    """Accept a completed automatic preflight compaction in the normal API flow.

    The normal chat endpoint invokes Context Engine for both intent routing and
    chat reply.  At high pressure either call-site may compact first; both are
    real automatic-preflight behavior and must keep the user on chat_reply.
    """
    return bool(
        record.get("route") == "chat_reply"
        and record.get("snapshot_found")
        and completed_compactions
    )


def _run_fresh_hard_probe(args: argparse.Namespace, run: LctRun, client: ApiClient) -> str:
    """Verify Hard automatic compaction without the 50-turn acceptance journey."""
    username = os.environ.get("PHASE2_E2E_USERNAME", "")
    password = os.environ.get("PHASE2_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD must be set by the PowerShell wrapper")

    conversation_id: str | None = None
    scenario: LiveScenario | None = None
    try:
        run.log("CTX-HARD-01: authenticate and create isolated normal-chat conversation")
        auth_step = run.step("authenticated real chat session", component="Auth API", expected="login succeeds without persisting credentials")
        auth = client.login(username, password)
        me = _data(client.request("GET", "/auth/me"))
        run.check(auth_step, bool(auth.get("authenticated")) and isinstance(me, dict), actual="authenticated owner-scoped API session")

        # A previously retained Hard checkpoint is diagnostic-only and must not
        # accumulate in the user's test account.  Deletion is deliberately
        # limited to the exact scenario ID after an owner-scoped GET verifies it.
        stale_checkpoint = _read_hard_checkpoint()
        if stale_checkpoint:
            stale_id = str(stale_checkpoint["conversation_id"])
            if _is_owned_scenario_conversation(client, stale_id):
                _data(client.request("DELETE", f"/conversations/{urllib.parse.quote(stale_id)}"))
                run.notes.append(f"STALE_HARD_CHECKPOINT_CLEANED: {stale_id}")
                _clear_hard_checkpoint()
            else:
                run.notes.append(f"STALE_HARD_CHECKPOINT_NOT_DELETED: ownership could not be verified for {stale_id}")

        conversation_id = _create_conversation(client, f"Context Hard probe {_stamp()}")
        scenario = LiveScenario(
            args=args,
            run=run,
            client=client,
            conversation_id=conversation_id,
            marker="CTX_HARD_PROBE_MARKER_NOT_ASSERTED",
        )

        run.log("CTX-HARD-01: verify the real normal-chat route before applying Hard pressure")
        route_probe = scenario.send(label="hard-probe-route", char_budget=args.route_probe_chars)
        route_step = run.step(
            "normal-chat route eligibility",
            component="Intent routing + Context Engine",
            expected="the real long normal-chat message reaches chat_reply and creates a fresh snapshot",
        )
        run.observe(
            route_step,
            route_probe.get("route") == "chat_reply" and bool(route_probe.get("snapshot_found")),
            actual=json.dumps(
                {
                    "route": route_probe.get("route"),
                    "intent": route_probe.get("intent"),
                    "intent_context_diagnostics": route_probe.get("intent_context_diagnostics"),
                    "chat_context_diagnostics": route_probe.get("chat_context_diagnostics"),
                },
                ensure_ascii=False,
            ),
            evidence={"route_probe": route_probe},
        )
        if route_probe.get("route") != "chat_reply" or not route_probe.get("snapshot_found"):
            return "TEST_FAIL"

        final_record = route_probe
        for index in range(1, args.hard_probe_load_turns + 1):
            run.log(
                f"CTX-HARD-01: send real calibrated pressure turn {index}/{args.hard_probe_load_turns}"
            )
            final_record = scenario.send(
                label=f"hard-probe-load-{index}",
                char_budget=args.hard_probe_payload_chars,
            )
            preflight = _preflight_or_empty(final_record)
            if str(preflight.get("waterline") or "") == "absolute":
                run.notes.append("HARD_PROBE_STOPPED_AT_ABSOLUTE: do not add more pressure turns")
                break
            compactions_now = _completed_preflight_compactions(
                _compaction_items(client, conversation_id)
            )
            if _fresh_hard_probe_passed(final_record, compactions_now):
                break

        compactions = _compaction_items(client, conversation_id)
        completed = _completed_preflight_compactions(compactions)
        hard_step = run.step(
            "Hard automatic compaction in normal chat",
            component="Context Engine + configured compression model",
            expected="real normal-chat traffic reaches chat_reply and persists a completed automatic preflight compaction",
        )
        run.observe(
            hard_step,
            _fresh_hard_probe_passed(final_record, completed),
            actual=json.dumps(
                {
                    "route": final_record.get("route"),
                    "intent": final_record.get("intent"),
                    "preflight": _preflight_or_empty(final_record),
                    "intent_context_diagnostics": final_record.get("intent_context_diagnostics"),
                    "chat_context_diagnostics": final_record.get("chat_context_diagnostics"),
                    "completed_preflight_run_count": len(completed),
                },
                ensure_ascii=False,
            ),
            evidence={"final_turn": final_record, "compaction_runs": compactions},
        )
        evidence_path = run.write_evidence(
            "fresh-hard-probe.json",
            {
                "scenario": "CTX-HARD-01",
                "conversation_id": conversation_id,
                "turn_count": scenario.turn_count,
                "route_probe": route_probe,
                "final_turn": final_record,
                "compaction_runs": compactions,
                "api_calls": client.calls,
            },
        )
        for item in run.steps:
            item.evidence.setdefault("scenario_evidence", evidence_path)
        return "LCT_PARTIAL_PASS" if not any(item.status == "FAIL" for item in run.steps) else "TEST_FAIL"
    finally:
        if conversation_id:
            cleanup: dict[str, Any] = {"conversation_id": conversation_id, "deleted": False}
            if args.keep_artifacts:
                cleanup["retention"] = "explicit_keep_artifacts"
            else:
                try:
                    _data(client.request("DELETE", f"/conversations/{urllib.parse.quote(conversation_id)}"))
                    cleanup["deleted"] = True
                except Exception as exc:  # cleanup must not hide test evidence
                    cleanup["cleanup_error"] = type(exc).__name__
            run.write_evidence("cleanup.json", cleanup)


def _run(args: argparse.Namespace, run: LctRun) -> str:
    username = os.environ.get("PHASE2_E2E_USERNAME", "")
    password = os.environ.get("PHASE2_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD must be set by the PowerShell wrapper")
    client = ApiClient(args.base_url, timeout_seconds=args.request_timeout_seconds)
    if args.fresh_hard_probe:
        return _run_fresh_hard_probe(args, run, client)
    if args.resume_hard_checkpoint:
        run.log("CTX-LONG-01: authenticate before owner-scoped Hard checkpoint retry")
        auth_step = run.step("authenticated real chat session", component="Auth API", expected="login succeeds without persisting credentials")
        auth = client.login(username, password)
        me = _data(client.request("GET", "/auth/me"))
        run.check(auth_step, bool(auth.get("authenticated")) and isinstance(me, dict), actual="authenticated owner-scoped API session")
        return _resume_hard_checkpoint(args, run, client)
    marker = f"CTX_LONG_MARKER_{_stamp()}"
    conversation_id: str | None = None
    scenario: LiveScenario | None = None
    full_scenario_passed = False
    hard_checkpoint_retained = False
    try:
        run.log("CTX-LONG-01: authenticate and create isolated normal-chat conversation")
        auth_step = run.step("authenticated real chat session", component="Auth API", expected="login succeeds without persisting credentials")
        auth = client.login(username, password)
        me = _data(client.request("GET", "/auth/me"))
        run.check(auth_step, bool(auth.get("authenticated")) and isinstance(me, dict), actual="authenticated owner-scoped API session")

        conversation_id = _create_conversation(client, f"Context waterline scenario {_stamp()}")
        scenario = LiveScenario(args=args, run=run, client=client, conversation_id=conversation_id, marker=marker)

        run.log("CTX-LONG-01: verify one realistic long journal turn remains on the normal chat route")
        route_probe = scenario.send(
            label="normal-chat-route-probe", char_budget=args.route_probe_chars
        )
        route_step = run.step(
            "long normal-chat route eligibility",
            component="Intent routing + Context Engine",
            expected="a realistic long journal message is routed to chat_reply and produces a chat.reply snapshot",
        )
        run.check(
            route_step,
            route_probe.get("route") == "chat_reply" and bool(route_probe.get("snapshot_found")),
            actual=json.dumps(
                {
                    "route": route_probe.get("route"),
                    "intent": route_probe.get("intent"),
                    "snapshot_public_id": route_probe.get("snapshot_public_id"),
                },
                ensure_ascii=False,
            ),
            evidence={"route_probe": route_probe},
        )
        if args.route_probe_only:
            run.notes.append(
                "ROUTE_PROBE_ONLY: normal-chat eligibility passed; Soft/Hard/Absolute "
                "waterlines and marker recall were intentionally not executed."
            )
            evidence_path = run.write_evidence(
                "route-probe.json",
                {
                    "scenario": "CTX-LONG-01",
                    "mode": "route_probe_only",
                    "conversation_id": conversation_id,
                    "marker_sha256_16": _short_hash(marker),
                    "route_probe": route_probe,
                    "api_calls": client.calls,
                },
            )
            for step in run.steps:
                step.evidence.setdefault("scenario_evidence", evidence_path)
            return "LCT_PARTIAL_PASS"

        run.log(f"CTX-LONG-01: send {args.warmup_turns} real normal-chat warmup turns")
        for index in range(args.warmup_turns):
            scenario.send(label=f"warmup-{index + 1}", char_budget=args.warmup_chars)
        baseline = scenario.records[-1]
        telemetry = baseline.get("preflight")
        if not isinstance(telemetry, dict):
            step = run.step("preflight telemetry available", component="Context Audit API", expected="owner-scoped chat snapshot contains content-free preflight telemetry")
            run.check(step, True, actual="telemetry unavailable; cannot certify waterlines", evidence={"latest_snapshot": _safe_snapshot_evidence(_latest_chat_snapshot(client, conversation_id))})
            run.notes.append("OBSERVABILITY_GAP: deployed backend does not expose preflight telemetry. Restart the backend after this change, then rerun.")
            return "OBSERVABILITY_GAP"

        baseline_step = run.step("target waterline normal chat", component="Context Engine preflight", expected="real chat remains below Soft and is passed without pruning")
        run.observe(
            baseline_step,
            telemetry.get("waterline") == "target" and telemetry.get("action") == "pass",
            actual=json.dumps(telemetry, ensure_ascii=False),
            evidence={"baseline": baseline},
        )

        soft = _attempt_stage(
            run=run,
            scenario=scenario,
            name="Soft waterline reachability",
            component="Context Engine preflight",
            expected="adaptive real-chat payload reaches Soft/PRUNE",
            operation=lambda: scenario.drive(
                name="soft", expected_waterline="soft", expected_action="prune"
            ),
        )
        soft_step = run.step("Soft waterline deterministic prune", component="Context Engine preflight", expected="actual Soft threshold with PRUNE, no compression-provider call")
        soft_pf = _preflight_or_empty(soft)
        run.observe(
            soft_step,
            soft_pf.get("tokens_before", 0) >= soft_pf.get("soft_threshold", 1)
            and soft_pf.get("action") == "prune"
            and soft_pf.get("compression_provider_call_count") == 0
            and soft_pf.get("tokens_after", 0) < soft_pf.get("tokens_before", 0),
            actual=json.dumps(soft_pf, ensure_ascii=False),
            evidence={"waterline_turn": soft},
        )

        hard_prune = _attempt_stage(
            run=run,
            scenario=scenario,
            name="Hard deterministic-prune reachability",
            component="Context Engine preflight",
            expected="adaptive real-chat payload reaches Hard/PRUNE",
            operation=lambda: scenario.drive(
                name="hard-prune", expected_waterline="hard_compact", expected_action="prune"
            ),
        )
        hard_prune_step = run.step("Hard waterline deterministic-first prune", component="Context Engine preflight", expected="actual Hard threshold first applies deterministic pruning when that alone reduces below Hard")
        hard_prune_pf = _preflight_or_empty(hard_prune)
        run.observe(
            hard_prune_step,
            hard_prune_pf.get("tokens_before", 0) >= hard_prune_pf.get("hard_threshold", 1)
            and hard_prune_pf.get("action") == "prune"
            and hard_prune_pf.get("tokens_after", 0) <= hard_prune_pf.get("hard_threshold", 0)
            and hard_prune_pf.get("compression_provider_call_count") == 0,
            actual=json.dumps(hard_prune_pf, ensure_ascii=False),
            evidence={"waterline_turn": hard_prune},
        )

        run.log("CTX-LONG-01: age the Soft/Hard probe payloads out of the 12-message raw source window")
        for index in range(13):
            scenario.send(label=f"age-before-hard-compact-{index + 1}", char_budget=args.warmup_chars)

        payload_budget_pf = hard_prune_pf or _preflight_or_empty(scenario.records[-1] if scenario.records else None)
        hard_payload_chars = scenario.calibrated_payload_chars(
            target_tokens=int(payload_budget_pf.get("hard_threshold") or 0) + 6000
        )
        run.log(
            "CTX-LONG-01: send three calibrated payloads so the post-prune set remains above Hard"
        )
        hard = _attempt_stage(
            run=run,
            scenario=scenario,
            name="Hard compaction reachability",
            component="Context Engine + configured compression model",
            expected="calibrated payload reaches Hard/COMPACT",
            operation=lambda: scenario.drive_fixed_payload(
                name="hard-compact",
                expected_waterline="hard_compact",
                expected_action="compact",
                char_budget=hard_payload_chars,
                marker_on_attempt=1,
            ),
        )
        hard_step = run.step("Hard waterline real compaction", component="Context Engine + configured compression model", expected="actual Hard threshold calls the compression model only after deterministic pruning cannot reduce below Hard")
        hard_pf = _preflight_or_empty(hard)
        run.observe(
            hard_step,
            hard_pf.get("tokens_before", 0) >= hard_pf.get("hard_threshold", 1)
            and hard_pf.get("tokens_before", 0) < hard_pf.get("absolute_threshold", float("inf"))
            and hard_pf.get("action") == "compact"
            and hard_pf.get("compression_provider_call_count") == 1
            and hard.get("route") == "chat_reply",
            actual=json.dumps(hard_pf, ensure_ascii=False),
            evidence={"waterline_turn": hard, "calibrated_payload_chars": hard_payload_chars},
        )

        if args.checkpoint_after_hard_diagnostic:
            previous_checkpoint = _read_hard_checkpoint()
            _write_hard_checkpoint(
                conversation_id=conversation_id,
                turn_count=scenario.turn_count,
                records=scenario.records,
                hard_payload_chars=hard_payload_chars,
            )
            hard_checkpoint_retained = True
            previous_id = (
                str(previous_checkpoint.get("conversation_id"))
                if isinstance(previous_checkpoint, dict) and previous_checkpoint.get("conversation_id")
                else None
            )
            if previous_id and previous_id != conversation_id and _is_owned_scenario_conversation(client, previous_id):
                try:
                    _data(client.request("DELETE", f"/conversations/{urllib.parse.quote(previous_id)}"))
                except Exception as exc:  # checkpoint replacement must keep the new diagnostic target
                    run.notes.append(f"PREVIOUS_HARD_CHECKPOINT_DELETE_FAILED:{type(exc).__name__}")
            evidence_path = run.write_evidence(
                "hard-diagnostic-checkpoint.json",
                {
                    "scenario": "CTX-LONG-01",
                    "mode": "checkpoint_after_hard_diagnostic",
                    "conversation_id": conversation_id,
                    "turn_count": scenario.turn_count,
                    "hard_payload_chars": hard_payload_chars,
                    "hard_turn": hard,
                    "checkpoint": str(_hard_checkpoint_state_path()),
                    "api_calls": client.calls,
                },
            )
            for item in run.steps:
                item.evidence.setdefault("scenario_evidence", evidence_path)
            run.notes.append("HARD_DIAGNOSTIC_CHECKPOINT_RETAINED: rerun with --resume-hard-checkpoint after repair; no warmups will be replayed.")
            return "LCT_PARTIAL_PASS" if not any(step.status == "FAIL" for step in run.steps) else "TEST_FAIL"

        run.log("CTX-LONG-01: age the Hard-compacted raw payloads out of the 12-message source window")
        for index in range(13):
            scenario.send(label=f"age-before-absolute-{index + 1}", char_budget=args.warmup_chars)

        absolute_budget_pf = hard_pf or _preflight_or_empty(scenario.records[-1] if scenario.records else None)
        absolute_payload_chars = scenario.calibrated_payload_chars(
            target_tokens=int(absolute_budget_pf.get("absolute_threshold") or 0) + 6000
        )
        run.log("CTX-LONG-01: send three calibrated payloads to reach Absolute")
        absolute = _attempt_stage(
            run=run,
            scenario=scenario,
            name="Absolute compaction reachability",
            component="Context Engine + configured compression model",
            expected="calibrated payload reaches Absolute/COMPACT",
            operation=lambda: scenario.drive_fixed_payload(
                name="absolute",
                expected_waterline="absolute",
                expected_action="compact",
                char_budget=absolute_payload_chars,
            ),
        )
        absolute_step = run.step("Absolute waterline forced compaction", component="Context Engine + configured compression model", expected="actual Absolute threshold triggers forced compaction and does not send an oversized business prompt")
        absolute_pf = _preflight_or_empty(absolute)
        run.observe(
            absolute_step,
            absolute_pf.get("tokens_before", 0) >= absolute_pf.get("absolute_threshold", 1)
            and absolute_pf.get("action") == "compact"
            and absolute_pf.get("compression_provider_call_count") == 1
            and absolute_pf.get("business_provider_call_count") == 0
            and absolute.get("route") == "chat_reply",
            actual=json.dumps(absolute_pf, ensure_ascii=False),
            evidence={"waterline_turn": absolute},
        )

        compactions = _compaction_items(client, conversation_id)
        audit_step = run.step("persisted preflight compaction audit", component="Compaction Audit API", expected="Hard and Absolute runs are persisted with trigger_type=preflight and completed status")
        completed_preflight = [item for item in compactions if item.get("trigger_type") == "preflight" and item.get("status") == "completed"]
        run.observe(audit_step, len(completed_preflight) >= 2, actual=json.dumps(compactions, ensure_ascii=False), evidence={"compaction_runs": compactions})

        run.log("CTX-LONG-01: age the marker out of the last 12 raw messages, then ask for it without repeating it")
        for index in range(13):
            scenario.send(label=f"age-marker-{index + 1}", char_budget=args.warmup_chars)
        recall = scenario.send(label="exact-marker-recall", char_budget=args.warmup_chars, recall_query=True)
        recall_step = run.step("post-compaction exact marker recall", component="Real model response", expected="assistant returns the early marker exactly after it has aged out of the recent-message window")
        run.observe(
            recall_step,
            bool(recall.get("reply_contains_marker")),
            actual=json.dumps({"reply_contains_marker": recall.get("reply_contains_marker"), "reply_sha256_16": recall.get("reply_sha256_16"), "reply_length": recall.get("reply_length")}, ensure_ascii=False),
            evidence={"recall_turn": recall},
        )

        evidence_path = run.write_evidence(
            "long-conversation-waterlines.json",
            {
                "scenario": "CTX-LONG-01",
                "conversation_id": conversation_id,
                "turn_count": scenario.turn_count,
                "marker_sha256_16": _short_hash(marker),
                "records": scenario.records,
                "compaction_runs": compactions,
                "api_calls": client.calls,
            },
        )
        for step in run.steps:
            step.evidence.setdefault("scenario_evidence", evidence_path)
        full_scenario_passed = not any(step.status == "FAIL" for step in run.steps)
        return "LCT_PASS" if full_scenario_passed else "TEST_FAIL"
    finally:
        cleanup: dict[str, Any] = {"conversation_id": conversation_id, "deleted": False}
        delete_current = bool(conversation_id) and not args.keep_artifacts and not hard_checkpoint_retained and (
            not args.retain_latest_passed_conversation or not full_scenario_passed
        )
        if delete_current:
            try:
                _data(client.request("DELETE", f"/conversations/{urllib.parse.quote(conversation_id)}"))
                cleanup["deleted"] = True
            except Exception as exc:  # cleanup must not hide functional evidence
                cleanup["cleanup_error"] = type(exc).__name__
        elif conversation_id and args.retain_latest_passed_conversation and full_scenario_passed:
            cleanup["retention"] = "latest_passed"
            previous_id = _read_latest_passed_conversation_id()
            if previous_id and previous_id != conversation_id:
                if _is_owned_scenario_conversation(client, previous_id):
                    try:
                        _data(client.request("DELETE", f"/conversations/{urllib.parse.quote(previous_id)}"))
                        cleanup["previous_conversation_id"] = previous_id
                        cleanup["previous_deleted"] = True
                    except Exception as exc:  # preservation of current evidence wins over old cleanup
                        cleanup["previous_conversation_id"] = previous_id
                        cleanup["previous_cleanup_error"] = type(exc).__name__
                else:
                    cleanup["previous_conversation_id"] = previous_id
                    cleanup["previous_deleted"] = False
                    cleanup["previous_cleanup_skipped"] = "not_owned_scenario_conversation"
            _write_latest_passed_conversation_id(conversation_id)
        elif conversation_id and hard_checkpoint_retained:
            cleanup["retention"] = "hard_diagnostic_checkpoint"
            cleanup["checkpoint"] = str(_hard_checkpoint_state_path())
        if scenario is not None:
            run.write_evidence("cleanup.json", cleanup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real normal-chat context waterline scenario")
    parser.add_argument("--base-url", default="http://localhost:8000/api")
    parser.add_argument("--warmup-turns", type=int, default=50)
    parser.add_argument("--warmup-chars", type=int, default=180)
    parser.add_argument("--max-turns", type=int, default=150)
    parser.add_argument(
        "--max-stage-attempts",
        type=int,
        default=5,
        help="bounded adaptive attempts per waterline; failures are recorded and later stages continue",
    )
    parser.add_argument("--min-payload-chars", type=int, default=1800)
    parser.add_argument(
        "--route-probe-chars",
        type=int,
        default=12000,
        help="known-safe long-message size used only for the cheap route/snapshot precondition",
    )
    parser.add_argument(
        "--max-payload-chars",
        type=int,
        default=105000,
        help="per-message ceiling for calibrated Hard/Absolute stages; the script derives actual sizes from observed live token growth",
    )
    parser.add_argument("--request-timeout-seconds", type=int, default=180)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument(
        "--retain-latest-passed-conversation",
        action="store_true",
        help="keep only the latest fully passed scenario conversation; failures are deleted after evidence is written",
    )
    parser.add_argument(
        "--route-probe-only",
        action="store_true",
        help="verify only long-message normal-chat routing before paying for the full waterline run",
    )
    parser.add_argument(
        "--checkpoint-after-hard-diagnostic",
        action="store_true",
        help="stop after the real Hard diagnostic and retain one owned checkpoint conversation for a low-cost retry after repair",
    )
    parser.add_argument(
        "--resume-hard-checkpoint",
        action="store_true",
        help="retry only Hard/COMPACT against the retained owner-scoped checkpoint; it does not certify the full scenario",
    )
    parser.add_argument(
        "--fresh-hard-probe",
        action="store_true",
        help="run a fresh, low-turn real API/model probe for automatic Hard compaction; this is diagnostic evidence, not the full 50-turn acceptance scenario",
    )
    parser.add_argument(
        "--hard-probe-payload-chars",
        type=int,
        default=83140,
        help="real message size derived from the prior live Hard calibration",
    )
    parser.add_argument(
        "--hard-probe-load-turns",
        type=int,
        default=3,
        help="bounded number of pressure turns for the fresh Hard probe",
    )
    args = parser.parse_args()
    retention_modes = sum(
        bool(value)
        for value in (
            args.keep_artifacts,
            args.retain_latest_passed_conversation,
            args.checkpoint_after_hard_diagnostic,
        )
    )
    if retention_modes > 1:
        parser.error("choose only one of --keep-artifacts, --retain-latest-passed-conversation, or --checkpoint-after-hard-diagnostic")
    if args.resume_hard_checkpoint and (args.route_probe_only or args.checkpoint_after_hard_diagnostic or args.fresh_hard_probe):
        parser.error("--resume-hard-checkpoint cannot be combined with route-probe, checkpoint, or fresh-Hard-probe modes")
    if args.fresh_hard_probe and (args.route_probe_only or args.checkpoint_after_hard_diagnostic):
        parser.error("--fresh-hard-probe cannot be combined with --route-probe-only or --checkpoint-after-hard-diagnostic")
    if not 50 <= args.warmup_turns <= 150:
        parser.error("--warmup-turns must be between 50 and 150")
    if not 1 <= args.hard_probe_load_turns <= 4:
        parser.error("--hard-probe-load-turns must be between 1 and 4")
    if args.hard_probe_payload_chars < args.min_payload_chars:
        parser.error("--hard-probe-payload-chars must be at least --min-payload-chars")
    if not args.fresh_hard_probe and args.max_turns < args.warmup_turns + 49:
        parser.error("--max-turns must leave room for the Hard/Absolute source-window aging and recall turns")
    lct = "CTX-HARD-01" if args.fresh_hard_probe else "CTX-LONG-01"
    classification = (
        "real normal-chat automatic-Hard-compaction diagnostic probe"
        if args.fresh_hard_probe
        else "real long-conversation automatic-preflight scenario"
    )
    run = LctRun(lct=lct, repo_root=repository_root(), execution_path="LIVE_NORMAL_CHAT_API + REAL_MODEL + OWNER_AUDIT")
    try:
        verdict = _run(args, run)
        return run.finish(lct_verdict=verdict, classification=classification)
    except Exception as exc:
        if isinstance(exc, ScenarioStop):
            run.notes.append("SCENARIO_LIMIT_OR_WATERLINE_FAILURE: " + str(exc))
        return cli_failure(run, exc, classification=classification)


if __name__ == "__main__":
    raise SystemExit(main())
