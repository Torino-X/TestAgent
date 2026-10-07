"""Intent router — LLM-based user-intent classifier (F013 + F014 + F016).

Maps a user message + current-conversation file state to a structured
``IntentResult`` describing what the user wants and how the message
service should dispatch the request.

Failure semantics: ``recognize`` never raises.  All exceptions are caught
at the boundary and converted to a safe ``CLARIFY`` result.  This
guarantees that an LLM outage can never accidentally trigger an
``AgentTask`` creation.

F014 closeout: ``recognize`` now routes the LLM call through
``LLMClient.generate_with_profile(INTENT_PROFILE)`` — the contract
layer owns the system prompt override, the strict-JSON parsing, and
the fallback-to-CLARIFY policy on parse failure.  The contract is
registered as ``app.llm.task_profiles.INTENT_PROFILE`` and surfaced
via the module-level ``INTENT_CONTRACT`` alias.

F016: ``recognize`` accepts an optional ``IntentContext`` that carries
recent conversation turns, file summaries, and the latest task summary.
When present, the prompt includes these as structured context so the LLM
can make informed routing decisions (e.g. existing_task_action).

CE-04: ``recognize`` 的 LLM 调用按 ``MIG_CHAT`` flag 路由。flag=false →
legacy ``LLMClient.generate_with_profile``；flag=true → 只走
ContextInvokerBridge（``generate()``），失败执行 failure policy（返回
CLARIFY），不静默回退 legacy。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

from app.agent.enums import IntentType, MessageRoute
from app.common.result_parser import ResultParseError, ResultParser
from app.integrations.llm_client import LLMClient, LLMClientError
from app.llm.errors import LLMProfileParseError
from app.llm.task_profiles import INTENT_PROFILE, LLMTaskProfile
from app.models.uploaded_file import UploadedFile
from app.schemas.context import IntentContext

logger = logging.getLogger(__name__)


# ── Output schema ─────────────────────────────────────────────────


class IntentResult(BaseModel):
    """Structured output of the intent router."""

    intent: IntentType
    route: MessageRoute
    supported: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    need_files: bool = False
    required_file_types: list[str] = Field(default_factory=list)
    missing_file_types: list[str] = Field(default_factory=list)
    reply_message: Optional[str] = None
    reply: Optional[str] = None  # Deprecated compatibility field; router does not draft chat text.
    extra_payload: dict[str, Any] = Field(default_factory=dict)


# ── System prompt ────────────────────────────────────────────────


_INTENT_SYSTEM_PROMPT = """你是 TestAgent 的意图路由器。分析用户消息，结合当前会话已上传的文件和上下文信息，输出严格 JSON（不要 markdown 围栏、不要任何解释性文字）。

枚举约束：
- intent ∈ {general_chat, test_plan_generation, test_case_generation, document_question, knowledge_question, result_modification, ppt_generation, excel_generation, unknown}
- route  ∈ {chat_reply, agent_task, ask_for_files, unsupported, clarify, existing_task_action}
- supported=true 表示该意图在当前版本可执行

路由规则：
1. 闲聊、问候、概念解释、咨询类问题 → intent=general_chat, route=chat_reply, supported=true
2. 明确要求"生成测试方案/编写测试方案/输出测试计划/生成测试文档" → intent=test_plan_generation
   - 已上传并确认 requirement_doc + test_plan_template → route=agent_task
   - 否则 → route=ask_for_files, reply_message 引导用户上传两类文件
3. 测试用例生成 → intent=test_case_generation, route=unsupported
4. PPT 生成 → intent=ppt_generation, route=unsupported
5. Excel 生成 → intent=excel_generation, route=unsupported
6. 知识库问答 / 公司制度 / 历史项目 → intent=knowledge_question, route=unsupported
7. 对已上传文档或已生成产物提问（不是要生成）→ intent=document_question, route=chat_reply, supported=true
8. 已存在任务的结果修改/补充 → intent=result_modification, route=agent_task
   (Phase 2.5 决策 A — 复用 RESULT_MODIFICATION,route=agent_task;上层 MessageService
    按 ``incremental_agent_enabled`` 条件 gate:开 → 走 Incremental subgraph;
    关 → 退回 chat_reply 引导用户。)
9. 无法判断 → intent=unknown, route=clarify
10. 不要因为用户上传了文件就自动生成，必须有明确生成意图
11. 不要因为用户说"帮我看看/处理一下"就创建 AgentTask
12. 只有 route=agent_task 时，后端才允许创建 AgentTask

F016 已有任务动作规则（优先级最高）：
13. 如果最近任务状态为 waiting_user_confirm，用户说"继续/确认/就按这个/开始生成"等 → route=existing_task_action, intent=general_chat
14. 如果最近任务状态为 running/generating/reviewing/exporting，用户说"继续/好了吗"等 → route=existing_task_action
15. 如果最近任务状态为 completed/failed，用户说"继续刚才任务" → route=chat_reply（任务已结束，走普通聊天引导）
16. 只有结合上下文明确用户要创建新测试方案且文件齐全，才进入 agent_task
17. 如果上下文不明确（只说"继续"但没有明确任务），保守处理 → route=clarify

优先级：已有任务动作(existing_task_action) > 明确创建新任务(agent_task) > 普通问题(chat_reply) > 模糊表达(clarify)

输出字段规则：
- 你只负责意图分类和路由，不生成完整聊天回复正文
- reply 字段必须设为 null；普通聊天正文由下游 ChatLLMService 单独生成
- reply_message 只用于 ask_for_files / clarify / unsupported 等需要短提示的路由
- route=chat_reply 时 reply_message 也设为 null
"""


# ── Defaults ──────────────────────────────────────────────────────


CLARIFY_FALLBACK_TEXT = (
    "我还不能确定你要执行哪类任务。"
    "你是想生成测试方案、生成测试用例，还是咨询某个问题？"
)


# ── Contract linkage (F014) ────────────────────────────────────────
#
# The router owns its classifier semantics (the system prompt above),
# so the profile used at call time is a model_copy of INTENT_PROFILE
# with the system_prompt filled in.  The contract type is reused from
# INTENT_PROFILE (strict JSON, no Markdown, fallback = CLARIFY JSON).

INTENT_CONTRACT = INTENT_PROFILE  # re-export for clarity

# Intent recognition is a lightweight routing step, not the conversation
# answerer.  Passing an arbitrarily large user message (and several equally
# large historical turns) through its strict-JSON call can make a normal chat
# request fall back to ``unknown/clarify`` before ChatLLMService receives it.
# Keep the beginning and end: users commonly state their purpose up front or
# ask the concrete question at the end.  The original message remains intact
# for the subsequent chat.reply call and its Context Engine composition.
# Intent routing only chooses a route; the complete message belongs to the
# downstream chat reply.  Keep this deliberately small so the strict JSON
# classifier remains reliable even when normal users paste a long journal.
_INTENT_CURRENT_MESSAGE_LIMIT_CHARS = 1_200
_INTENT_SUMMARY_LIMIT_CHARS = 1_200
_INTENT_RECENT_TURN_LIMIT_CHARS = 600
_INTENT_RECENT_TURNS_LIMIT = 6


def _bound_for_intent(text: str, *, limit: int) -> str:
    """Bound routing-only input while preserving purpose-bearing head/tail."""
    value = str(text or "")
    if len(value) <= limit:
        return value
    head = max(1, int(limit * 0.7))
    tail = max(1, limit - head)
    return (
        value[:head]
        + "\n[内容过长：中间部分仅对意图路由省略；原始消息会完整交给后续聊天]\n"
        + value[-tail:]
    )


def _intent_current_message_id(intent_context: IntentContext | None) -> int | None:
    raw = getattr(intent_context, "current_message_id", None)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None

# Module-level helper to build a per-call profile that carries our
# actual system prompt but inherits the rest of the contract from
# INTENT_PROFILE (parser type, fallback JSON, max_tokens, etc.).
def _build_intent_profile() -> LLMTaskProfile:
    return INTENT_PROFILE.model_copy(update={"system_prompt": _INTENT_SYSTEM_PROMPT})


# ── Router ────────────────────────────────────────────────────────


class IntentRouter:
    """LLM-based user-intent classifier.

    Use ``recognize(content, files)`` to obtain a safe ``IntentResult``.
    The router guarantees no exception is ever raised to the caller; a
    successful LLM call is preferred, but any failure is converted to a
    ``CLARIFY`` result.

    F014 closeout: the underlying LLM call is dispatched through
    ``LLMClient.generate_with_profile(INTENT_PROFILE)`` so that the
    contract layer owns:
      * the strict-JSON parser (``JsonStrictParser``),
      * the fallback-to-CLARIFY policy on parse failure,
      * the max_tokens / temperature knobs from the profile.
    The router keeps its existing failure semantics (low-confidence
    override, file-presence enforcement done upstream in
    ``MessageService``).

    CE-04: 可注入 ``context_llm_invoker``（ContextInvokerBridge）。MIG_CHAT
    flag=true 时走 bridge（失败 → CLARIFY，不回退 legacy）；flag=false 走
    legacy LLMClient。
    """

    # Whitelist of file types we recognise.  Anything else is dropped
    # from ``required_file_types``/``missing_file_types``.
    _KNOWN_FILE_TYPES: frozenset[str] = frozenset(
        {"requirement_doc", "test_plan_template"}
    )

    def __init__(
        self,
        llm_client: LLMClient,
        result_parser: Optional[ResultParser] = None,
        context_llm_invoker: Any = None,
        task_flag_resolver: Any = None,
        session_factory: Any = None,
    ) -> None:
        self._llm = llm_client
        # ``result_parser`` is retained as a constructor parameter for
        # backwards compatibility but no longer used to parse the LLM
        # response directly — the contract layer's JsonStrictParser
        # owns parsing.  We keep the field in case callers pass one in
        # for legacy reasons; if they do we silently ignore it.
        self._parser = result_parser or ResultParser()
        self._profile = _build_intent_profile()
        # CE-04: ContextInvokerBridge。MIG_CHAT=true 时走 bridge；flag=false
        # 走 legacy LLMClient。bridge 不可用 → 显式失败策略（CLARIFY），不回退 legacy。
        self._bridge = context_llm_invoker or (
            llm_client if getattr(llm_client, "is_context_engine_bridge", False) else None
        )
        # CE-05 WP-2: 任务级 Flag Resolver（冻结 Manifest）；None → 进程级。
        self._task_flag_resolver = task_flag_resolver
        # WP-BE-09: 普通 Chat 路径走 bridge 时需要 runtime_context（snapshot
        # 落库 session_factory + llm_client）。AgentTask 路径由
        # ProductionRuntimeContextFactory 提供；普通 Chat 用注入的 session_factory。
        self._session_factory = session_factory

    async def recognize(
        self,
        content: str,
        files: Iterable[UploadedFile] | None = None,
        attached_file_ids: Optional[list[str]] = None,
        intent_context: Optional[IntentContext] = None,
        user_internal_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
        conversation_public_id: Optional[str] = None,
        context_workspace_key: Optional[str] = None,
    ) -> IntentResult:
        """Classify ``content`` and return a structured ``IntentResult``."""
        original_message_chars = len(content or "")
        routing_input_chars = 0
        routing_current_message_bounded = (
            original_message_chars > _INTENT_CURRENT_MESSAGE_LIMIT_CHARS
        )
        bridge_diagnostic: dict[str, Any] = {}

        def record_bridge_diagnostic(
            *,
            outcome: str,
            snapshot_public_id: str | None = None,
            exception: BaseException | None = None,
        ) -> None:
            """Keep content-free Context Engine evidence on a route result."""
            bridge_diagnostic.clear()
            bridge_diagnostic.update({"path": "context_bridge", "outcome": outcome})
            if snapshot_public_id:
                bridge_diagnostic["snapshot_public_id"] = str(snapshot_public_id)
            if exception is None:
                return
            bridge_diagnostic["exception_type"] = type(exception).__name__
            safe_error = getattr(exception, "error", None)
            error_code = getattr(safe_error, "code", None)
            error_stage = getattr(safe_error, "stage", None)
            if error_code:
                bridge_diagnostic["error_code"] = str(error_code)
            if error_stage:
                bridge_diagnostic["stage"] = str(getattr(error_stage, "value", error_stage))
            if safe_error is not None:
                bridge_diagnostic["retryable"] = bool(getattr(safe_error, "retryable", False))
                metadata = getattr(safe_error, "safe_metadata", {})
                if isinstance(metadata, dict):
                    for key in (
                        "reason", "compaction_attempted", "compaction_compactor_available",
                        "compaction_runtime_context_available", "compaction_phase",
                        "compaction_exception_type", "compaction_exception_code",
                    ):
                        if key in metadata:
                            bridge_diagnostic[key] = metadata[key]

        def with_routing_diagnostics(result: IntentResult) -> IntentResult:
            """Attach safe size-only diagnostics to the persisted reply payload.

            These values intentionally contain no user text.  They let a live
            scenario distinguish an old backend process from an active routing
            bound when a provider returns the conservative clarify fallback.
            """
            extra_payload = dict(result.extra_payload)
            extra_payload.update(
                {
                    "intent_routing_input_chars": routing_input_chars,
                    "intent_routing_original_message_chars": original_message_chars,
                    "intent_routing_current_message_bounded": routing_current_message_bounded,
                }
            )
            if bridge_diagnostic:
                extra_payload["intent_context_engine"] = dict(bridge_diagnostic)
            return result.model_copy(update={"extra_payload": extra_payload})

        using_context_bridge = False
        try:
            user_content = self._build_user_content(
                content or "", files or [], set(attached_file_ids or []),
                intent_context=intent_context,
            )
            routing_input_chars = len(user_content)
            logger.info(
                "IntentRouter.recognize: 调用LLM | 内容长度=%d | files=%d | attached=%d | has_context=%s",
                len(content or ""), len(list(files or [])), len(attached_file_ids or []),
                intent_context is not None,
            )
            # CE-04 MIG_CHAT 路由：flag=false → legacy；flag=true → 只走 bridge。
            # bridge 不可用/失败 → 显式 failure 策略（CLARIFY），不静默回退 legacy。
            from app.context_engine.feature_flags import get_context_engine_flags

            # CE-05 WP-2：任务路径经 task_flag_resolver 读 MIG_CHAT（冻结 Manifest）
            mig_chat = True
            if mig_chat:
                using_context_bridge = True
                if self._bridge is None or not getattr(
                    self._bridge, "available", False
                ):
                    record_bridge_diagnostic(outcome="bridge_unavailable")
                    logger.warning(
                        "IntentRouter: MIG_CHAT=true 但 Invoker 不可用"
                    )
                    logger.warning(
                        "FALLBACK_USED | component=intent_router | "
                        "from=context_bridge | to=clarify_result | "
                        "reason=bridge_unavailable | user_id=%s",
                        user_internal_id,
                    )
                    return with_routing_diagnostics(
                        self._clarify_result("intent_router_bridge_unavailable")
                    )
                bres = await self._bridge.generate(
                    user_id=user_internal_id or 0,
                    call_site="intent.recognize",
                    llm_task_profile=self._profile,
                    current_goal=user_content,
                    current_user_message_id=_intent_current_message_id(intent_context),
                    output_contract="json",
                    user_content=user_content,
                    conversation_id=conversation_id,
                    runtime_context=self._build_runtime_context(
                        user_internal_id,
                        conversation_id=conversation_id,
                        conversation_public_id=conversation_public_id,
                        context_workspace_key=context_workspace_key,
                    ),
                )
                if bres is None or bres.value is None:
                    record_bridge_diagnostic(outcome="bridge_empty_result")
                    logger.warning(
                        "IntentRouter: MIG_CHAT bridge 返回空值（无 value）"
                    )
                    logger.warning(
                        "FALLBACK_USED | component=intent_router | "
                        "from=context_bridge | to=clarify_result | "
                        "reason=bridge_value_none | user_id=%s",
                        user_internal_id,
                    )
                    return with_routing_diagnostics(
                        self._clarify_result("intent_router_bridge_value_none")
                    )
                # .parsed 即 Invoker 解析后的 dict（json_strict）
                record_bridge_diagnostic(
                    outcome="completed",
                    snapshot_public_id=getattr(bres, "snapshot_public_id", None),
                )
                raw = bres.as_profile_result()
            else:
                return with_routing_diagnostics(
                    self._clarify_result("intent_router_legacy_route_retired")
                )
                # F014 contract layer: JsonStrictParser yields a dict (or
                # the fallback JSON is parsed to a dict).  On parse failure
                # the wrapper already substituted the fallback JSON, so we
                # only need to handle transport-level errors here.
            payload = raw.parsed
            if not isinstance(payload, dict):
                # Defensive: contract layer should never return non-dict
                # for INTENT_PROFILE; if it does, treat as parse failure.
                logger.warning(
                    "IntentRouter: profile result not a dict "
                    "(success=%s, error_type=%s, parsed_type=%s)",
                    getattr(raw, "success", None),
                    getattr(raw, "error_type", None)
                    or getattr(raw, "error_message", None),
                    type(payload).__name__,
                )
                logger.warning(
                    "FALLBACK_USED | component=intent_router | "
                    "from=intent_profile | to=clarify_result | "
                    "reason=profile_result_not_dict | success=%s | parsed_type=%s",
                    getattr(raw, "success", None),
                    type(payload).__name__,
                )
                return with_routing_diagnostics(self._clarify_result(
                    f"intent_router_parse_failed: profile returned {type(payload).__name__}"
                ))
            if (
                payload.get("reason") == "intent_parse_failed"
                and self._is_explicit_direct_chat(content or "")
            ):
                logger.warning(
                    "IntentRouter: intent JSON parse failed; explicit direct-chat request uses safe chat fallback"
                )
                return with_routing_diagnostics(
                    self._direct_chat_result("intent_parse_failed_explicit_direct_chat")
                )
            built = self._build_result(payload)
            logger.info(
                "IntentRouter.recognize: 结果 | intent=%s | route=%s | confidence=%.2f | reason=%s",
                built.intent.value, built.route.value, built.confidence, built.reason,
            )
            return with_routing_diagnostics(built)
        except LLMClientError as exc:
            if using_context_bridge:
                record_bridge_diagnostic(outcome="bridge_exception", exception=exc)
            logger.warning(
                "FALLBACK_USED | component=intent_router | "
                "from=llm_intent_call | to=clarify_result | "
                "reason=llm_client_error | err_type=%s | err=%s",
                type(exc).__name__,
                str(exc)[:300],
            )
            return with_routing_diagnostics(self._clarify_result(
                f"intent_router_llm_error: {exc.__class__.__name__}"
            ))
        except LLMProfileParseError as exc:
            if using_context_bridge:
                record_bridge_diagnostic(outcome="bridge_exception", exception=exc)
            # INTENT_PROFILE has on_parse_failure=FALLBACK_DEFAULT, so
            # this should not normally surface — but if a future profile
            # switches to RAISE we still want a safe clarify fallback.
            logger.warning(
                "FALLBACK_USED | component=intent_router | "
                "from=intent_profile | to=clarify_result | "
                "reason=profile_parse_error | err_type=%s | err=%s",
                type(exc).__name__,
                str(exc)[:300],
            )
            return with_routing_diagnostics(self._clarify_result(
                f"intent_router_parse_failed: {exc.__class__.__name__}"
            ))
        except ResultParseError as exc:
            if using_context_bridge:
                record_bridge_diagnostic(outcome="bridge_exception", exception=exc)
            # Defensive: in case the contract layer ever delegates back
            # to the legacy ``ResultParser`` path.
            logger.warning(
                "FALLBACK_USED | component=intent_router | "
                "from=legacy_result_parser | to=clarify_result | "
                "reason=result_parse_error | err_type=%s | err=%s",
                type(exc).__name__,
                str(exc)[:300],
            )
            return with_routing_diagnostics(
                self._clarify_result(f"intent_router_parse_failed: {exc}")
            )
        except Exception as exc:  # noqa: BLE001 — belt-and-braces
            if using_context_bridge:
                record_bridge_diagnostic(outcome="bridge_exception", exception=exc)
            logger.exception(
                "FALLBACK_USED | component=intent_router | "
                "from=intent_router | to=clarify_result | "
                "reason=unexpected_failure | err_type=%s | err=%s",
                type(exc).__name__,
                str(exc)[:300],
            )
            return with_routing_diagnostics(self._clarify_result(
                f"intent_router_failure: {exc.__class__.__name__}"
            ))

    # ── Internals ─────────────────────────────────────────────────

    def _build_runtime_context(
        self,
        user_internal_id: int | None,
        *,
        conversation_id: int | None = None,
        conversation_public_id: str | None = None,
        context_workspace_key: str | None = None,
    ):
        """构造普通 Chat 路径的轻量 runtime_context（bridge 需要）。

        AgentTask 路径由 ProductionRuntimeContextFactory 提供完整 runtime_context；
        普通 Chat 无此 factory，这里提供 session_factory + llm_client + user_internal_id
        满足 ContextAwareLLMInvoker 的 snapshot 落库与 provider 调用。
        """
        if self._session_factory is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            session_factory=self._session_factory,
            llm_client=self._llm,
            user_internal_id=int(user_internal_id or 0),
            conversation_internal_id=conversation_id,
            conversation_public_id=conversation_public_id,
            context_workspace_key=(str(context_workspace_key).strip() or None)
            if context_workspace_key
            else None,
        )

    @staticmethod
    def _build_user_content(
        content: str, files: Iterable[UploadedFile], attached_ids: set[str],
        *,
        intent_context: Optional[IntentContext] = None,
    ) -> str:
        bounded_content = _bound_for_intent(
            content, limit=_INTENT_CURRENT_MESSAGE_LIMIT_CHARS
        )
        # F016: When intent_context is available, inject conversation
        # history, file summaries, and task summary into the prompt.
        if intent_context is not None:
            return IntentRouter._build_context_user_content(
                bounded_content, files, attached_ids, intent_context
            )

        lines: list[str] = [f"用户消息：{bounded_content}"]
        if attached_ids:
            lines.append("")
            lines.append(
                "用户在本次消息中明确附加的文件 ID："
                + "、".join(sorted(attached_ids))
            )
        file_lines: list[str] = []
        for f in files:
            file_lines.append(
                f"- public_id={getattr(f, 'public_id', '')} "
                f"original_name={getattr(f, 'original_name', '')} "
                f"file_type={getattr(f, 'file_type', '')} "
                f"upload_status={getattr(f, 'upload_status', '')}"
            )
        if file_lines:
            lines.append("")
            lines.append("已上传文件：")
            lines.extend(file_lines)
        return "\n".join(lines)

    @staticmethod
    def _build_context_user_content(
        content: str,
        files: Iterable[UploadedFile],
        attached_ids: set[str],
        intent_context: IntentContext,
    ) -> str:
        """Build intent prompt with F016 context (recent turns, files, task).

        Current user message is placed FIRST for maximum visibility.
        Context sections follow as supporting information.
        """
        parts: list[str] = []

        # Current user message — ALWAYS FIRST for prominence
        parts.append(f"【当前用户消息】\n{content}")

        # Conversation summary
        if intent_context.conversation_summary:
            parts.append(
                "【会话摘要】\n"
                + _bound_for_intent(
                    intent_context.conversation_summary,
                    limit=_INTENT_SUMMARY_LIMIT_CHARS,
                )
            )

        # Recent conversation turns
        if intent_context.recent_turns:
            parts.append("【最近对话（仅作背景参考）】")
            for turn in intent_context.recent_turns[-_INTENT_RECENT_TURNS_LIMIT:]:
                label = "用户" if turn.role == "user" else "助手"
                parts.append(
                    f"{label}："
                    + _bound_for_intent(
                        turn.content,
                        limit=_INTENT_RECENT_TURN_LIMIT_CHARS,
                    )
                )

        # File summaries from context
        if intent_context.file_summaries:
            parts.append("【当前会话文件摘要】")
            for f in intent_context.file_summaries:
                parts.append(
                    f"- file_id={f.file_id} "
                    f"file_name={f.file_name} "
                    f"file_type={f.file_type or 'unknown'} "
                    f"upload_status={f.upload_status or 'unknown'}"
                )

        # Explicitly attached files this turn
        if attached_ids:
            parts.append(
                "用户在本次消息中明确附加的文件 ID："
                + "、".join(sorted(attached_ids))
            )

        # Current files (from UploadedFile objects — legacy path)
        file_lines: list[str] = []
        for f in files:
            file_lines.append(
                f"- public_id={getattr(f, 'public_id', '')} "
                f"original_name={getattr(f, 'original_name', '')} "
                f"file_type={getattr(f, 'file_type', '')} "
                f"upload_status={getattr(f, 'upload_status', '')}"
            )
        if file_lines:
            parts.append("已上传文件（完整列表）：")
            parts.extend(file_lines)

        # Latest task summary
        if intent_context.latest_task_summary:
            t = intent_context.latest_task_summary
            task_info = f"任务类型：{t.task_type or 'test_plan_generation'}；状态：{t.status}"
            if t.pending_confirmation_count > 0:
                task_info += f"；待确认：{t.pending_confirmation_count} 项"
            parts.append(f"【最近任务状态】\n{task_info}")

        return "\n\n".join(parts)

    def _build_result(self, payload: dict[str, Any]) -> IntentResult:
        intent = self._match_enum(
            payload.get("intent"), IntentType, default=IntentType.UNKNOWN
        )
        route = self._match_enum(
            payload.get("route"), MessageRoute, default=MessageRoute.CLARIFY
        )
        supported = bool(payload.get("supported", False))
        confidence = self._clamp_float(payload.get("confidence", 0.0))
        reason = str(payload.get("reason", ""))
        need_files = bool(payload.get("need_files", False))
        required = self._filter_file_types(payload.get("required_file_types"))
        missing = self._filter_file_types(payload.get("missing_file_types"))
        reply_message = payload.get("reply_message")
        if reply_message is not None:
            reply_message = str(reply_message).strip() or None

        # Merged reply: only valid for chat_reply route
        reply = payload.get("reply")
        if reply is not None:
            reply = str(reply).strip() or None

        result = IntentResult(
            intent=intent,
            route=route,
            supported=supported,
            confidence=confidence,
            reason=reason,
            need_files=need_files,
            required_file_types=required,
            missing_file_types=missing,
            reply_message=reply_message,
            reply=reply,
        )

        # Fix LLM returning 0 for confidence (not properly filled).
        # If the LLM recognized a specific intent (not unknown), apply
        # a default confidence so the low-confidence downgrade doesn't
        # incorrectly fire.
        if result.confidence == 0.0 and result.intent != IntentType.UNKNOWN:
            result = result.model_copy(update={"confidence": 0.8})
            logger.info(
                "IntentRouter: confidence=0 且 intent=%s，修正为 0.8",
                result.intent.value,
            )

        # Low-confidence override: never trust a low-confidence agent_task.
        # Phase 2.5: 对 result_modification 仍按 agent_task 处理;上层 MessageService
        # 依据 ``incremental_agent_enabled`` 决定走 Incremental subgraph 还是 chat_reply。
        if (
            result.confidence < 0.5
            and result.route == MessageRoute.AGENT_TASK
            and result.intent != IntentType.RESULT_MODIFICATION
        ):
            logger.info(
                "IntentRouter: 低置信度降级 agent_task→clarify | confidence=%.2f",
                result.confidence,
            )
            return result.model_copy(
                update={
                    "route": MessageRoute.CLARIFY,
                    "reply_message": reply_message or CLARIFY_FALLBACK_TEXT,
                }
            )
        # Low-confidence general_chat is fine — chat is the safe default.
        # Conversation-scoped document questions use the same safe chat route,
        # where the Context Engine retrieves versioned document evidence.  Keep
        # this invariant here as well as in the classifier prompt so an older
        # model response cannot reinstate the legacy static “unsupported” copy.
        if result.intent == IntentType.DOCUMENT_QUESTION:
            return result.model_copy(
                update={
                    "route": MessageRoute.CHAT_REPLY,
                    "supported": True,
                    "reply_message": None,
                }
            )
        return result

    @staticmethod
    def _match_enum(value: Any, enum_cls: type, default: Any) -> Any:
        if value is None:
            return default
        try:
            text = str(value).strip().lower()
        except Exception:
            return default
        # Accept both enum members and their string values
        for member in enum_cls:
            if str(member.value).lower() == text or member.name.lower() == text:
                return member
        return default

    @staticmethod
    def _clamp_float(value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v

    def _filter_file_types(self, value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            t = str(item).strip()
            if t in self._KNOWN_FILE_TYPES and t not in out:
                out.append(t)
        return out

    @staticmethod
    def _clarify_result(reason: str) -> IntentResult:
        return IntentResult(
            intent=IntentType.UNKNOWN,
            route=MessageRoute.CLARIFY,
            supported=False,
            confidence=0.0,
            reason=reason,
            reply_message=CLARIFY_FALLBACK_TEXT,
        )

    @staticmethod
    def _direct_chat_result(reason: str) -> IntentResult:
        """Safe fallback for an explicit request that cannot create work."""
        return IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=1.0,
            reason=reason,
        )

    @staticmethod
    def _is_explicit_direct_chat(content: str) -> bool:
        """Recognise a narrow no-side-effect consultation after JSON fallback."""
        normalized = "".join(content.split())
        return (
            "不生成任务" in normalized
            and "测试方案" in normalized
            and "其他产物" in normalized
            and any(token in normalized for token in ("请直接", "说明", "分析", "回答", "讨论"))
        )


# 模块定位:IntentRouter (Phase 2.9A F013) — LLM 驱动的 5 路由分流
#
# 这是 MessageService 的"中枢分流层"。LLM 根据 prompt + attachments + 历史消息
# 决定走哪条路由 (chat_reply / ask_for_files / unsupported / clarify / agent_task),
# 取代早期的 deterministic intent_recognizer。
#
# 链路 (MessageService 主路径):
#   send_message → IntentRouter.classify_with_confidence(prompt, ctx)
#     → 返回 IntentDecision(intent=..., confidence=..., reason=..., reply_message=...)
#   MessageService 读 intent 路由:
#     chat_reply     → ChatLLMService
#     ask_for_files  → static reply(文件缺失列表)
#     unsupported    → static reply by intent
#     clarify        → router.reply_message
#     agent_task     → AgentTaskService.create_task
#
# 关键约束:
#   - 必须返回 confidence 字段;MessageService 用阈值判断降级 clarify;
#   - 不调用 ChatLLMService(那是下一跳);
#   - prompt 必须包含 conversation summary 而非全量消息;
#   - profile:INTENT_ROUTER_PROFILE(便宜模型,降低每请求开销);
#   - 失败 fallback:context_recorder.Fallback(message="...")
#
# 重构提示(本注释提示尚未实施):
#   IntentRouter 当前承担太多责任(LLM call + 决策 + 回复草稿),
#   计划拆出:
#     1. IntentClassifier(只产出 intent + confidence)
#     2. ReplyDraftService(草拟 5 路由的默认回复,LLM-free)
#   这能让 MessageService 和 IntentRouter 各自职责清晰。
