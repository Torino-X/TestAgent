"""F023 — RetryPolicy: orchestrator-level self-healing for tool failures.

Pure decision tree — no I/O, no LLM calls, no prompt construction.
Inputs are the tool's last result dict (containing ``error.code`` and
``error.recoverable``) plus the current attempt number.  Output is a
:class:`RetryDecision` that tells the caller whether to retry, how long
to wait, and (optionally) what new inputs to use on the next attempt.

The prompt-feedback assembly (i.e. "given N failed attempts, what
should the LLM see") lives in :mod:`app.agent.retry_feedback` — that
module gets the accumulated history built by the orchestrator.

Error-class taxonomy (also documented in :data:`RECOVERABLE_ERROR_CODES`,
:data:`SCHEMA_ERROR_CODES`, :data:`NETWORK_ERROR_CODES`,
:data:`DEGRADABLE_ERROR_CODES`):

  * Unrecoverable (no retry) — user config errors, missing files,
    template contract violations.  These surface to the user
    immediately.
  * Schema (feedback retry) — JSON_VALIDATION_FAILED, schema mismatch.
    Caller injects corrective prompt block on retry.
  * Network (backoff retry) — MODEL_TIMEOUT, MODEL_STATUS_ERROR,
    MODEL_CONNECTION_ERROR.  Same inputs, exponential backoff.
  * Degradable (degraded-input retry) — VISION_FAILED, OCR_FAILED.
    Caller strips the offending image and retries with OCR-only / skip.
  * Unknown / recoverable=True — defaults to same-input retry with
    small backoff.

Backoff is exponential: ``0.5 * 2 ** (attempt - 1)`` seconds.  Three
retries produce sleeps of 0.5s, 1s, 2s — total 3.5s of pure waiting,
on top of three LLM calls (the dominant cost).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List


# ── Error-code classification ────────────────────────────────────────


#: Default backoff when the error class doesn't pin a specific delay.
DEFAULT_BACKOFF_SECONDS: float = 0.5

#: Backoff for schema errors — short, the LLM just needs feedback.
SCHEMA_BACKOFF_SECONDS: float = 0.5

#: Backoff for degradable errors — a touch longer (we may be re-reading).
DEGRADE_BACKOFF_SECONDS: float = 1.0

#: Hard ceiling on retry attempts regardless of caller configuration.
ABSOLUTE_MAX_RETRIES: int = 5


# Errors that mean "the model returned structurally wrong data".
# Retry with feedback-injected prompt — the caller patches in the
# corrective block before the next LLM call.
SCHEMA_ERROR_CODES: FrozenSet[str] = frozenset({
    "JSON_VALIDATION_FAILED",
    "JSON_TRUNCATED",
    "JSON_PARSE_ERROR",
    "SCHEMA_MISMATCH",
})

# Errors that mean "transport or upstream went wrong, try again".
# Same inputs, exponential backoff.
NETWORK_ERROR_CODES: FrozenSet[str] = frozenset({
    "MODEL_TIMEOUT",
    "MODEL_STATUS_ERROR",
    "MODEL_CONNECTION_ERROR",
    "MODEL_UNKNOWN_ERROR",
    "LLM_NOT_CONFIGURED",  # config may flip mid-task (re-login etc.)
})

# Errors that mean "one image blew up but the rest are fine".
# Caller rebuilds inputs without the failed resource.
DEGRADABLE_ERROR_CODES: FrozenSet[str] = frozenset({
    "VISION_FAILED",
    "OCR_FAILED",
    "IMAGE_PROCESSING_FAILED",
    "KNOWLEDGE_UNAVAILABLE",
})

# Errors that mean "the user must fix something, retrying won't help".
# Hard stop — never retry even if the tool mistakenly tagged them
# as recoverable=True.
UNRECOVERABLE_ERROR_CODES: FrozenSet[str] = frozenset({
    "REQUIREMENT_FILE_NOT_FOUND",
    "TEMPLATE_FILE_NOT_FOUND",
    "EXPORT_TEMPLATE_NOT_FOUND",
    "MISSING_REQUIREMENT",
    "MISSING_TEMPLATE",
    "MISSING_GENERATION_CONFIG",
    "NO_AI_FIELDS",
    "MODEL_CONFIG_ERROR",  # user must configure
    "WORD_EXPORT_CONTRACT_ERROR",  # operator must fix template
    "INTEGRITY_CHECK_FAILED",
    "EXPORT_TEMPLATE_INVALID",
    "TEMPLATE_PARSE_FAILED",
    "REQUIREMENT_PARSE_FAILED",
})


# ── Decision object ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryDecision:
    """Orchestrator's verdict on what to do next.

    Attributes:
        should_retry: True if the caller should invoke the tool again.
        attempt: 1-based attempt number for the NEXT run (i.e. if the
            current attempt was 1 and we decide to retry, attempt=2).
        max_retries: The configured ceiling — useful for SSE payloads.
        backoff_seconds: How long the caller should sleep before the
            next attempt (0 when ``should_retry`` is False).
        new_inputs: Replacement inputs for the next attempt.  Empty
            dict means "reuse the previous inputs verbatim".
        strategy: One of ``"schema_feedback"``, ``"backoff"``,
            ``"degrade"``, ``"same_inputs"``, ``"hard_stop"``.  Purely
            for logging / SSE.
        reason: Human-readable explanation; surfaced in the
            ``retrying`` SSE event payload.
    """

    should_retry: bool
    attempt: int
    max_retries: int
    backoff_seconds: float
    new_inputs: Dict[str, Any] = field(default_factory=dict)
    strategy: str = ""
    reason: str = ""

    def to_sse_payload(self, tool_name: str) -> Dict[str, Any]:
        """Serialize for inclusion in an AgentEvent payload."""
        return {
            "tool_name": tool_name,
            "attempt": self.attempt,
            "max_retries": self.max_retries,
            "backoff_seconds": self.backoff_seconds,
            "strategy": self.strategy,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RetryContext:
    """Per-attempt retry metadata passed to a tool's ``run()`` method.

    Tools that don't care about retries can ignore this argument
    entirely — the contract is purely additive: when the orchestrator
    decides to retry, it builds a new RetryContext with the latest
    failure info; on the first call, ``attempt=1`` and the error list
    is empty.

    Attributes:
        attempt: 1-based attempt number for THIS call (matches the
            ``attempt`` value the policy saw when deciding to retry).
        max_retries: The configured ceiling — useful for tools that
            want to do their own internal retries (e.g. one more
            continuation on JSON truncation).
        previous_errors: Failed attempts so far, oldest first.  Each
            entry is a tool-result ``error`` dict (code/message/
            recoverable).  Empty on the first attempt.
        hints: Free-form per-tool guidance from the policy.  Currently
            holds the degraded-input markers built by
            :meth:`RetryPolicy._build_degraded_inputs` (``_retry_hints``
            is also forwarded through the inputs dict for backward
            compat with tools that already read from there).
        strategy: The strategy that produced this retry (``schema_feedback``
            / ``backoff`` / ``degrade`` / ``same_inputs``).  Empty on
            the first attempt.

    Mutability: frozen.  Tools that want to share state between
    attempts should use the orchestrator's checkpoint store instead.
    """

    attempt: int
    max_retries: int
    previous_errors: List[Dict[str, Any]] = field(default_factory=list)
    hints: Dict[str, Any] = field(default_factory=dict)
    strategy: str = ""

    @property
    def is_retry(self) -> bool:
        """True if this is not the first attempt."""
        return self.attempt > 1

    def last_error(self) -> Dict[str, Any]:
        """Return the most recent error dict, or {} if first attempt."""
        if not self.previous_errors:
            return {}
        return self.previous_errors[-1]

    def last_error_code(self) -> str:
        """Shortcut for the previous error's code, or '' if first attempt."""
        return str(self.last_error().get("code", "") or "")

    def to_log_dict(self) -> Dict[str, Any]:
        """Compact dict for structured logging."""
        return {
            "attempt": self.attempt,
            "max_retries": self.max_retries,
            "strategy": self.strategy,
            "is_retry": self.is_retry,
            "previous_error_count": len(self.previous_errors),
        }


# ── Policy ────────────────────────────────────────────────────────────


class RetryPolicy:
    """Decide whether (and how) to retry a failed tool invocation.

    Stateless: the caller owns the retry history and feeds it via
    ``attempt`` number.  This class never mutates caller state.
    """

    def __init__(self, max_retries: int = 3) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if max_retries > ABSOLUTE_MAX_RETRIES:
            raise ValueError(
                f"max_retries={max_retries} exceeds absolute ceiling "
                f"{ABSOLUTE_MAX_RETRIES}"
            )
        self._max_retries = max_retries

    @property
    def max_retries(self) -> int:
        return self._max_retries

    def decide(
        self,
        tool_name: str,
        attempt: int,
        tool_result: Dict[str, Any],
        current_inputs: Dict[str, Any] | None = None,
    ) -> RetryDecision:
        """Inspect a failed tool result and decide the next step.

        Args:
            tool_name: Name of the tool (used only for logging payloads).
            attempt: 1-based count of attempts already made (the call
                that just returned ``tool_result`` is ``attempt``).
            tool_result: The tool's standard result dict.
            current_inputs: Inputs that produced ``tool_result``.  Used
                by degradable-error strategy to rebuild a stripped
                input set; ignored otherwise.

        Returns:
            :class:`RetryDecision`.
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")

        # First-call sanity: a successful result is never a retry
        # candidate regardless of what other fields claim.
        if tool_result.get("success") is True:
            return RetryDecision(
                should_retry=False,
                attempt=attempt,
                max_retries=self._max_retries,
                backoff_seconds=0.0,
                strategy="hard_stop",
                reason="tool 已成功，无需重试",
            )

        error = tool_result.get("error") or {}
        if not isinstance(error, dict):
            error = {}

        err_code = str(error.get("code", "") or "")
        err_msg = str(error.get("message", "") or "")
        recoverable = bool(error.get("recoverable", False))

        # ── Hard-stop conditions ──────────────────────────────────
        # 1. Hard-coded unrecoverable codes always win.
        if err_code in UNRECOVERABLE_ERROR_CODES:
            return RetryDecision(
                should_retry=False,
                attempt=attempt,
                max_retries=self._max_retries,
                backoff_seconds=0.0,
                strategy="hard_stop",
                reason=f"错误码 {err_code} 不可重试",
            )

        # 2. Tool explicitly tagged the error non-recoverable.
        if not recoverable:
            return RetryDecision(
                should_retry=False,
                attempt=attempt,
                max_retries=self._max_retries,
                backoff_seconds=0.0,
                strategy="hard_stop",
                reason=f"工具标记 non-recoverable: {err_code or '未知错误'}",
            )

        # 3. Already at the ceiling — fail with a clear reason so
        #    callers can surface "已达最大重试次数".
        if attempt > self._max_retries:
            return RetryDecision(
                should_retry=False,
                attempt=attempt,
                max_retries=self._max_retries,
                backoff_seconds=0.0,
                strategy="hard_stop",
                reason=(
                    f"已达最大重试次数 {self._max_retries}，"
                    f"最后一次错误: {err_code}"
                ),
            )

        # ── Retry strategies (in priority order) ──────────────────
        next_attempt = attempt + 1

        if err_code in SCHEMA_ERROR_CODES:
            return RetryDecision(
                should_retry=True,
                attempt=next_attempt,
                max_retries=self._max_retries,
                backoff_seconds=SCHEMA_BACKOFF_SECONDS,
                strategy="schema_feedback",
                reason=f"schema 错误 ({err_code})，下次注入修正 prompt",
            )

        if err_code in DEGRADABLE_ERROR_CODES:
            new_inputs = self._build_degraded_inputs(
                tool_name, err_code, err_msg, current_inputs or {},
            )
            return RetryDecision(
                should_retry=True,
                attempt=next_attempt,
                max_retries=self._max_retries,
                backoff_seconds=DEGRADE_BACKOFF_SECONDS,
                new_inputs=new_inputs,
                strategy="degrade",
                reason=f"降级重试 ({err_code})：跳过失败资源",
            )

        if err_code in NETWORK_ERROR_CODES:
            backoff = DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1))
            return RetryDecision(
                should_retry=True,
                attempt=next_attempt,
                max_retries=self._max_retries,
                backoff_seconds=backoff,
                strategy="backoff",
                reason=f"网络/上游错误 ({err_code})，指数退避 {backoff:.1f}s",
            )

        # Fallback: recoverable=True but unknown code — gentle retry.
        return RetryDecision(
            should_retry=True,
            attempt=next_attempt,
            max_retries=self._max_retries,
            backoff_seconds=DEFAULT_BACKOFF_SECONDS,
            strategy="same_inputs",
            reason=(
                f"未分类可重试错误 ({err_code or '?'})，"
                f"同入参再试一次"
            ),
        )

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _build_degraded_inputs(
        tool_name: str,
        err_code: str,
        err_msg: str,
        current_inputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Rebuild inputs by stripping the offending resource.

        Image-understanding degradation: when a vision/OCR error pops
        up, we drop the image index from the configured image list so
        the next call only OCRs the survivors.  Other tools get a
        ``_retry_strategy: degrade`` hint the caller can inspect.

        The exact mechanism is intentionally tool-specific — the
        default policy returns a metadata-only patch that downstream
        tools can read via ``inputs.get("_retry_hints", {})``.
        """
        degraded = dict(current_inputs)
        hints = dict(degraded.get("_retry_hints", {}))
        hints.update({
            "last_strategy": "degrade",
            "last_degrade_reason": err_code,
            "last_degrade_message": err_msg[:200],
            "degrade_attempt": int(hints.get("degrade_attempt", 0)) + 1,
        })
        degraded["_retry_hints"] = hints

        if tool_name == "RequirementParserTool" and err_code in {
            "VISION_FAILED", "OCR_FAILED", "IMAGE_PROCESSING_FAILED",
        }:
            # Hand a `failed_image_indices` list to the parser so it
            # can OCR-only or skip those images on the next pass.
            failed = list(hints.get("failed_image_indices", []))
            # We don't know which specific image blew up from the
            # error alone; the tool will filter via image_count cap.
            degraded["failed_image_indices"] = failed
            degraded["enable_in_doc_parsing"] = False

        if tool_name == "KnowledgeSearchTool" and err_code == "KNOWLEDGE_UNAVAILABLE":
            # Force the orchestrator-level skip on the next pass
            # (the tool itself can also short-circuit; this is a
            # belt-and-braces signal for the caller).
            degraded["force_skip"] = True

        return degraded

# 模块定位:F023 RetryPolicy — orchestrator 层面 self-healing 决策
#
# 纯决策树,**无 I/O,无 LLM,无 prompt**:根据 tool envelope(成功 / 失败 / 错误码)
# 决定下一步 (retry_plan / schema_feedback / escalate)。
#
# 链路:
#   orchestrator 拿到 tool envelope
#     → RetryPolicy.decide(envelope, retry_context)
#       → RetryDecision(action=retry/skip/escalate, strategy=…)
#
# RetryStrategy 枚举:
#   - retry_with_clean_state / retry_with_kb_hint
#   - schema_feedback (重 prompt 强制字段对齐)
#   - escalate_to_repair_agent (Phase 2.4)
#
# 关键约束:
#   - 决策是纯函数,**可单测**;
#   - 不依赖外部资源(否则注入长延迟);
#   - 与 BudgetTracker 联动(budget 超限 → escalate)。
