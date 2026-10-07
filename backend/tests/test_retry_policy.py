"""Unit tests for F023 RetryPolicy.

Covers:
  * Successful result → no retry (defensive guard)
  * Unrecoverable error codes → hard stop
  * Tool-tagged non-recoverable → hard stop
  * Attempt ceiling → hard stop
  * Schema errors → feedback strategy
  * Network errors → exponential backoff
  * Degradable errors → degrade strategy with input hint
  * Unknown recoverable → same_inputs fallback
  * Backoff math (0.5, 1, 2 sequence)
  * SSE payload shape
  * Constructor validation (negative / over ceiling)
"""

from __future__ import annotations

import pytest

from app.agent.retry_policy import (
    ABSOLUTE_MAX_RETRIES,
    DEFAULT_BACKOFF_SECONDS,
    DEGRADE_BACKOFF_SECONDS,
    DEGRADABLE_ERROR_CODES,
    NETWORK_ERROR_CODES,
    RetryDecision,
    RetryPolicy,
    SCHEMA_BACKOFF_SECONDS,
    SCHEMA_ERROR_CODES,
    UNRECOVERABLE_ERROR_CODES,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _err_result(code: str, recoverable: bool = True, message: str = "boom") -> dict:
    return {
        "success": False,
        "tool_name": "TestTool",
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "recoverable": recoverable,
        },
    }


def _ok_result() -> dict:
    return {
        "success": True,
        "tool_name": "TestTool",
        "data": {"hello": "world"},
        "error": None,
    }


# ── Constructor ──────────────────────────────────────────────────────


class TestRetryPolicyInit:
    def test_default_max_retries_is_three(self):
        assert RetryPolicy().max_retries == 3

    def test_negative_max_retries_rejected(self):
        with pytest.raises(ValueError, match="must be >= 0"):
            RetryPolicy(max_retries=-1)

    def test_over_ceiling_rejected(self):
        with pytest.raises(ValueError, match="exceeds absolute ceiling"):
            RetryPolicy(max_retries=ABSOLUTE_MAX_RETRIES + 1)

    def test_zero_is_allowed(self):
        """Zero retries = first failure is final; useful for tests."""
        assert RetryPolicy(max_retries=0).max_retries == 0


# ── Hard-stop cases ──────────────────────────────────────────────────


class TestHardStop:
    def test_successful_result_never_retries(self):
        policy = RetryPolicy()
        d = policy.decide("AnyTool", 1, _ok_result())
        assert d.should_retry is False
        assert d.strategy == "hard_stop"
        assert d.backoff_seconds == 0.0

    @pytest.mark.parametrize("code", sorted(UNRECOVERABLE_ERROR_CODES))
    def test_unrecoverable_codes_stop_even_if_tagged_recoverable(self, code):
        """Hard-coded list overrides recoverable=True to prevent
        accidental retry on user-config / template-contract errors."""
        policy = RetryPolicy(max_retries=3)
        d = policy.decide(
            "AnyTool", 1, _err_result(code, recoverable=True),
        )
        assert d.should_retry is False
        assert d.strategy == "hard_stop"
        assert code in d.reason

    def test_tool_tagged_non_recoverable_stops(self):
        policy = RetryPolicy()
        d = policy.decide(
            "AnyTool", 1, _err_result("SOMETHING_WEIRD", recoverable=False),
        )
        assert d.should_retry is False
        assert "non-recoverable" in d.reason

    def test_attempt_ceiling_stops(self):
        policy = RetryPolicy(max_retries=2)
        # attempt=3 already exceeds ceiling of 2
        d = policy.decide(
            "AnyTool", 3, _err_result("MODEL_TIMEOUT"),
        )
        assert d.should_retry is False
        assert d.strategy == "hard_stop"
        assert "已达最大重试次数 2" in d.reason

    def test_attempt_equals_ceiling_still_retries_one_more(self):
        """attempt=3 with max=3 means we've made 3 attempts; the
        next would be the 4th, which is the last allowed retry."""
        policy = RetryPolicy(max_retries=3)
        d = policy.decide(
            "AnyTool", 3, _err_result("MODEL_TIMEOUT"),
        )
        assert d.should_retry is True
        assert d.attempt == 4

    def test_zero_max_retries_hard_stops_immediately(self):
        policy = RetryPolicy(max_retries=0)
        d = policy.decide(
            "AnyTool", 1, _err_result("MODEL_TIMEOUT"),
        )
        assert d.should_retry is False

    def test_missing_error_dict_does_not_crash(self):
        """Robustness: a tool that forgot to fill `error` shouldn't
        bring down the orchestrator.  Hard stop with reason."""
        policy = RetryPolicy()
        d = policy.decide("AnyTool", 1, {"success": False, "error": None})
        assert d.should_retry is False
        assert d.strategy == "hard_stop"

    def test_attempt_zero_rejected(self):
        policy = RetryPolicy()
        with pytest.raises(ValueError, match="attempt must be >= 1"):
            policy.decide("AnyTool", 0, _err_result("MODEL_TIMEOUT"))


# ── Schema strategy ──────────────────────────────────────────────────


class TestSchemaStrategy:
    @pytest.mark.parametrize("code", sorted(SCHEMA_ERROR_CODES))
    def test_schema_codes_trigger_feedback(self, code):
        policy = RetryPolicy(max_retries=3)
        d = policy.decide("TestPlanGeneratorTool", 1, _err_result(code))
        assert d.should_retry is True
        assert d.strategy == "schema_feedback"
        assert d.backoff_seconds == SCHEMA_BACKOFF_SECONDS
        assert code in d.reason

    def test_schema_attempt_advances(self):
        policy = RetryPolicy(max_retries=3)
        d = policy.decide(
            "TestPlanGeneratorTool", 2, _err_result("JSON_VALIDATION_FAILED"),
        )
        assert d.should_retry is True
        assert d.attempt == 3

    def test_schema_keeps_original_inputs_empty(self):
        """The schema strategy doesn't mutate inputs itself — the
        orchestrator injects feedback via a separate mechanism."""
        policy = RetryPolicy()
        d = policy.decide(
            "TestPlanGeneratorTool", 1, _err_result("JSON_VALIDATION_FAILED"),
            current_inputs={"requirement_text": "x"},
        )
        assert d.new_inputs == {}


# ── Network strategy ─────────────────────────────────────────────────


class TestNetworkStrategy:
    @pytest.mark.parametrize("code", sorted(NETWORK_ERROR_CODES))
    def test_network_codes_trigger_backoff(self, code):
        policy = RetryPolicy(max_retries=3)
        d = policy.decide("TestPlanGeneratorTool", 1, _err_result(code))
        assert d.should_retry is True
        assert d.strategy == "backoff"
        assert code in d.reason

    def test_backoff_sequence(self):
        """0.5, 1.0, 2.0 on attempts 1, 2, 3."""
        policy = RetryPolicy(max_retries=3)
        d1 = policy.decide("Tool", 1, _err_result("MODEL_TIMEOUT"))
        d2 = policy.decide("Tool", 2, _err_result("MODEL_TIMEOUT"))
        d3 = policy.decide("Tool", 3, _err_result("MODEL_TIMEOUT"))
        assert d1.backoff_seconds == 0.5
        assert d2.backoff_seconds == 1.0
        assert d3.backoff_seconds == 2.0

    def test_network_keeps_original_inputs_empty(self):
        policy = RetryPolicy()
        d = policy.decide(
            "Tool", 1, _err_result("MODEL_TIMEOUT"),
            current_inputs={"x": 1},
        )
        assert d.new_inputs == {}


# ── Degradable strategy ──────────────────────────────────────────────


class TestDegradeStrategy:
    @pytest.mark.parametrize("code", sorted(DEGRADABLE_ERROR_CODES))
    def test_degradable_codes_trigger_degrade(self, code):
        policy = RetryPolicy(max_retries=3)
        d = policy.decide(
            "RequirementParserTool", 1, _err_result(code),
            current_inputs={"requirement_file_id": "f1"},
        )
        assert d.should_retry is True
        assert d.strategy == "degrade"
        assert code in d.reason
        assert d.backoff_seconds == DEGRADE_BACKOFF_SECONDS

    def test_degrade_injects_retry_hints(self):
        policy = RetryPolicy()
        d = policy.decide(
            "RequirementParserTool", 1, _err_result("VISION_FAILED"),
            current_inputs={"requirement_file_id": "f1"},
        )
        hints = d.new_inputs.get("_retry_hints", {})
        assert hints.get("last_strategy") == "degrade"
        assert hints.get("last_degrade_reason") == "VISION_FAILED"
        assert hints.get("degrade_attempt") == 1

    def test_degrade_hint_increments(self):
        """Calling decide twice accumulates degrade_attempt — the
        orchestrator passes the previous inputs back so we can count."""
        policy = RetryPolicy()
        first_inputs = {"_retry_hints": {"degrade_attempt": 1}}
        d = policy.decide(
            "RequirementParserTool", 2, _err_result("VISION_FAILED"),
            current_inputs=first_inputs,
        )
        assert d.new_inputs["_retry_hints"]["degrade_attempt"] == 2

    def test_requirement_parser_degrade_disables_vision(self):
        policy = RetryPolicy()
        d = policy.decide(
            "RequirementParserTool", 1, _err_result("VISION_FAILED"),
            current_inputs={"requirement_file_id": "f1"},
        )
        assert d.new_inputs.get("enable_in_doc_parsing") is False

    def test_kb_unavailable_sets_force_skip(self):
        policy = RetryPolicy()
        d = policy.decide(
            "KnowledgeSearchTool", 1, _err_result("KNOWLEDGE_UNAVAILABLE"),
            current_inputs={"query": "test"},
        )
        assert d.new_inputs.get("force_skip") is True

    def test_other_tools_get_generic_degrade_hints(self):
        policy = RetryPolicy()
        d = policy.decide(
            "WordExportTool", 1, _err_result("IMAGE_PROCESSING_FAILED"),
            current_inputs={"file_id": "abc"},
        )
        assert "_retry_hints" in d.new_inputs
        assert d.new_inputs.get("enable_in_doc_parsing") is None  # not set
        assert d.new_inputs.get("force_skip") is None  # not set


# ── Unknown-code fallback ────────────────────────────────────────────


class TestUnknownFallback:
    def test_unknown_recoverable_uses_same_inputs(self):
        policy = RetryPolicy()
        d = policy.decide(
            "AnyTool", 1, _err_result("SOMETHING_NEW", recoverable=True),
        )
        assert d.should_retry is True
        assert d.strategy == "same_inputs"
        assert d.backoff_seconds == DEFAULT_BACKOFF_SECONDS

    def test_unknown_recoverable_keeps_inputs_empty(self):
        policy = RetryPolicy()
        d = policy.decide(
            "AnyTool", 1, _err_result("SOMETHING_NEW", recoverable=True),
            current_inputs={"x": 1},
        )
        assert d.new_inputs == {}


# ── Decision payload / serialization ─────────────────────────────────


class TestRetryDecisionSerialization:
    def test_to_sse_payload_shape(self):
        d = RetryDecision(
            should_retry=True,
            attempt=2,
            max_retries=3,
            backoff_seconds=0.5,
            strategy="schema_feedback",
            reason="schema 错误",
        )
        payload = d.to_sse_payload("TestPlanGeneratorTool")
        assert payload["tool_name"] == "TestPlanGeneratorTool"
        assert payload["attempt"] == 2
        assert payload["max_retries"] == 3
        assert payload["backoff_seconds"] == 0.5
        assert payload["strategy"] == "schema_feedback"
        assert payload["reason"] == "schema 错误"

    def test_decision_is_frozen(self):
        d = RetryDecision(
            should_retry=True,
            attempt=2,
            max_retries=3,
            backoff_seconds=0.5,
        )
        with pytest.raises(Exception):
            d.should_retry = False  # type: ignore[misc]


# ── Decision integration: typical scenarios ──────────────────────────


class TestTypicalScenarios:
    def test_test_plan_generator_schema_loop(self):
        """Simulate the LLM returning invalid headers 3 times — should
        retry with feedback each time, then hard-stop on the 4th call."""
        policy = RetryPolicy(max_retries=3)
        # Attempt 1 fails schema
        d1 = policy.decide(
            "TestPlanGeneratorTool", 1, _err_result("JSON_VALIDATION_FAILED"),
        )
        assert d1.should_retry is True
        assert d1.attempt == 2
        # Attempt 2 fails schema again
        d2 = policy.decide(
            "TestPlanGeneratorTool", 2, _err_result("JSON_VALIDATION_FAILED"),
        )
        assert d2.should_retry is True
        assert d2.attempt == 3
        # Attempt 3 fails schema
        d3 = policy.decide(
            "TestPlanGeneratorTool", 3, _err_result("JSON_VALIDATION_FAILED"),
        )
        assert d3.should_retry is True
        assert d3.attempt == 4
        # Attempt 4 (last allowed retry already used) fails again
        d4 = policy.decide(
            "TestPlanGeneratorTool", 4, _err_result("JSON_VALIDATION_FAILED"),
        )
        assert d4.should_retry is False
        assert d4.strategy == "hard_stop"

    def test_requirement_parser_vision_degrade_then_succeeds(self):
        """Vision fails → degrade hint; orchestrator re-runs with
        OCR-only enabled, parser succeeds (test only covers decide())."""
        policy = RetryPolicy()
        d = policy.decide(
            "RequirementParserTool", 1, _err_result("VISION_FAILED"),
            current_inputs={"requirement_file_id": "f1"},
        )
        assert d.should_retry is True
        assert d.strategy == "degrade"
        assert d.new_inputs["enable_in_doc_parsing"] is False

    def test_template_parse_failure_no_retry(self):
        """Template is structurally broken — user must fix the file."""
        policy = RetryPolicy()
        d = policy.decide(
            "TemplateParserTool", 1,
            _err_result("TEMPLATE_PARSE_FAILED", recoverable=False),
        )
        assert d.should_retry is False

    def test_word_export_contract_violation_no_retry(self):
        """Template contract violation: operator must fix template."""
        policy = RetryPolicy()
        d = policy.decide(
            "WordExportTool", 1,
            _err_result("WORD_EXPORT_CONTRACT_ERROR", recoverable=True),
        )
        assert d.should_retry is False  # hard-coded unrecoverable wins


# ── Run ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])