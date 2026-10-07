"""Message service — send messages, route by intent, create agent tasks.

F013: replaced the binary "files present / missing" branching with a
full 5-route dispatch driven by an LLM-based ``IntentRouter``:

  - ``chat_reply``      → ``ChatLLMService.generate_reply``
  - ``ask_for_files``   → static default text + missing-file list
  - ``unsupported``     → static default text keyed by intent
  - ``clarify``         → router's reply_message (or default)
  - ``agent_task``      → create ``AgentTask`` (only when files are
                          complete; otherwise the deterministic
                          ``_apply_file_requirement_override`` downgrades
                          the route to ``ask_for_files``)

The user message is **always** written and returned.  The assistant
message is written for all non-task routes; for ``agent_task`` the
assistant's first reply comes via the SSE stream and is not duplicated
here.

Backward compatibility: ``agent_task`` and ``agent_reply`` legacy fields
remain populated identically.  New ``route``/``intent``/``requires_sse``/
``task_id`` fields are additive and default to "" / False / None.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar, Optional, Pattern

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.enums import IntentType, MessageRoute
from app.agent.capability_registry import CapabilityRegistry
from app.agent.capability_router import CapabilityRouter
from app.agent.file_requirement_checker import FileRequirementChecker
from app.agent.intent_router import IntentResult, IntentRouter
from app.agent.request_understanding import build_request_understanding
from app.agent.retrieval_planner import (
    KnowledgeMode,
    RetrievalPlan,
    RetrievalPlanner,
    normalize_knowledge_mode,
)
from app.core.config import get_settings
from app.core.exceptions import (
    TemplateNotFoundError,
    TemplateUseCopyFailedError,
    TemplateVersionMissingError,
)
from app.integrations.llm_client import LLMClient
from app.llm.task_profiles import CHAT_FALLBACK_REPLY as _CHAT_FALLBACK_REPLY_SRC, TITLE_PROFILE
from app.models.agent_event import AgentEvent
from app.models.agent_task import AgentTask
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.agent_task_repository import AgentTaskRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.event_repository import EventRepository
from app.repositories.file_repository import FileRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.agent_execution_request_repository import (
    AgentExecutionRequestRepository,
)
from app.repositories.attachment_understanding_repository import (
    MessageAttachmentRepository,
    TaskFileBindingRepository,
)
from app.schemas.context import ChatContext, IntentContext, TaskTriggerContext
from app.services.chat_llm_service import ChatLLMService
from app.services.conversation_context_service import ConversationContextService
from app.services.conversation_summary_service import ConversationSummaryService
from app.services.context_snapshot_service import ContextSnapshotService
from app.services.knowledge_answer_service import KnowledgeAnswerService
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id
from app.common.json_utils import normalize_json_object

logger = logging.getLogger(__name__)

# Canonical default titles used to decide whether a brand-new
# conversation's title still needs auto-generation.  All values are
# pure ASCII / CJK without mojibake — the old garbled entries
# ("鏂颁細璇?", "鏂版祴璇曚换鍔?") were copy-paste artefacts that
# prevented proper title generation on otherwise-empty titles.
DEFAULT_CONVERSATION_TITLES = {
    "新会话",
    "新对话",
    "新测试任务",
    "新的对话",
    "",
}

# Backwards-compatible alias — keep the symbol exported because other
# modules (e.g. tests) may import it.
TITLE_GENERATION_SYSTEM_PROMPT = TITLE_PROFILE.system_prompt


# ── Default reply texts (verbatim from F013 spec) ────────────────


DEFAULT_ASK_FOR_FILES_TEXT = (
    "可以，我可以帮你生成测试方案。"
    "请先上传需求文档和测试方案模板。上传完成后，"
    "我会先解析文档和模板，再让你确认章节处理方式，最后生成可下载的 Word 测试方案。"
)

DEFAULT_CLARIFY_TEXT = (
    "我还不能确定你要执行哪类任务。"
    "你是想生成测试方案、生成测试用例，还是咨询某个问题？"
)

INTENT_DEFAULT_REPLIES: dict[IntentType, str] = {
    IntentType.TEST_CASE_GENERATION: (
        "测试用例生成功能将在后续版本开放。"
        "当前版本已支持基于需求文档和测试方案模板生成测试方案。"
    ),
    IntentType.PPT_GENERATION: (
        "PPT 生成功能当前阶段暂未开放。"
        "当前版本主要支持测试方案 Word 文档生成。"
    ),
    IntentType.EXCEL_GENERATION: (
        "Excel 生成功能当前阶段暂未开放。"
    ),
    IntentType.KNOWLEDGE_QUESTION: (
        "知识库问答功能将在后续接入公司知识库后开放。"
        "当前版本主要支持测试方案生成。"
    ),
    IntentType.DOCUMENT_QUESTION: (
        "文档问答功能将在后续版本开放。"
        "当前版本主要支持基于需求文档和测试方案模板生成测试方案。"
    ),
    IntentType.RESULT_MODIFICATION: (
        "已收到你的修改请求。请告诉我需要修改哪一部分内容。"
    ),
    IntentType.UNKNOWN: DEFAULT_CLARIFY_TEXT,
}

# Re-export of the canonical chat fallback.  Single source of truth
# lives in ``app.llm.task_profiles.CHAT_FALLBACK_REPLY``.  Both this
# module and ``chat_llm_service`` import from there, breaking the
# historical import cycle (they cannot import each other directly).
CHAT_FALLBACK_REPLY: str = _CHAT_FALLBACK_REPLY_SRC

STRICT_MAAS_NO_HIT_TEXT = "公司知识库中未检索到相关内容。"
STRICT_MAAS_UNAVAILABLE_TEXT = "公司知识库当前不可用，请稍后重试。"


_REASONING_TAGS = (
    "think",
    "thinking",
    "reasoning",
    "analysis",
    "thought",
    "thoughts",
    "scratchpad",
    "chain_of_thought",
    "cot",
)

_REASONING_START_RE = re.compile(
    r"<\s*(?P<xml_tag>"
    + "|".join(re.escape(tag) for tag in _REASONING_TAGS)
    + r")\b[^>]*>"
    + r"|\[\s*(?P<bracket_tag>"
    + "|".join(re.escape(tag) for tag in _REASONING_TAGS)
    + r")\s*\]"
    + r"|<\|begin_of_(?P<pipe_tag>thought|thinking|reasoning|analysis)\|>",
    re.IGNORECASE | re.DOTALL,
)

_MAX_REASONING_DELIMITER_LEN = 64


class ReasoningBlockFilter:
    """Streaming-safe filter for hidden model reasoning blocks.

    The filter only removes explicit paired reasoning delimiters. It
    intentionally keeps natural-language labels such as "思考：" or
    "分析：" because those may be legitimate user-facing text.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._end_re: Pattern[str] | None = None

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buffer += chunk
        visible: list[str] = []

        while True:
            if self._end_re is not None:
                end_match = self._end_re.search(self._buffer)
                if end_match is None:
                    self._buffer = self._buffer[-_MAX_REASONING_DELIMITER_LEN:]
                    return "".join(visible)
                self._buffer = self._buffer[end_match.end():]
                self._end_re = None
                continue

            start_match = _REASONING_START_RE.search(self._buffer)
            if start_match is None:
                safe_len = self._safe_visible_prefix_len(self._buffer)
                if safe_len <= 0:
                    return "".join(visible)
                visible.append(self._buffer[:safe_len])
                self._buffer = self._buffer[safe_len:]
                return "".join(visible)

            visible.append(self._buffer[:start_match.start()])
            self._end_re = self._end_pattern_for(start_match)
            self._buffer = self._buffer[start_match.end():]

    def finish(self) -> str:
        if self._end_re is not None:
            self._buffer = ""
            self._end_re = None
            return ""
        visible = self._buffer
        self._buffer = ""
        return visible

    @classmethod
    def strip_text(cls, text: str) -> str:
        filter_ = cls()
        return (filter_.feed(text) + filter_.finish()).strip()

    @staticmethod
    def _end_pattern_for(start_match: re.Match[str]) -> Pattern[str]:
        xml_tag = start_match.group("xml_tag")
        if xml_tag:
            return re.compile(rf"</\s*{re.escape(xml_tag)}\s*>", re.IGNORECASE)

        bracket_tag = start_match.group("bracket_tag")
        if bracket_tag:
            return re.compile(rf"\[/\s*{re.escape(bracket_tag)}\s*\]", re.IGNORECASE)

        pipe_tag = start_match.group("pipe_tag") or ""
        return re.compile(rf"<\|end_of_{re.escape(pipe_tag)}\|>", re.IGNORECASE)

    @staticmethod
    def _safe_visible_prefix_len(buffer: str) -> int:
        marker_pos = max(buffer.rfind("<"), buffer.rfind("["))
        if marker_pos < 0:
            return len(buffer)
        keep_len = len(buffer) - marker_pos
        if keep_len > _MAX_REASONING_DELIMITER_LEN:
            return len(buffer) - _MAX_REASONING_DELIMITER_LEN
        return marker_pos


# ── Service ──────────────────────────────────────────────────────


class MessageService:
    """Message business logic with intent-routing (F013)."""

    REQUIRED_FILE_TYPES = frozenset(
        FileRequirementChecker.REQUIRED_FOR_TEST_PLAN
    )
    # A MessageService is constructed per HTTP request.  Summary maintenance,
    # however, belongs to the conversation rather than a request instance.
    # Keep this process-local registry so rapid normal-chat turns cannot start
    # multiple competing summary LLM calls before the first summary is saved.
    _summary_maintenance_tasks: ClassVar[dict[int, asyncio.Task[Any]]] = {}

    def __init__(
        self,
        session: AsyncSession,
        *,
        llm_client: Optional[LLMClient] = None,
        intent_router: Optional[IntentRouter] = None,
        chat_service: Optional[ChatLLMService] = None,
        context_llm_invoker: Optional[Any] = None,
        context_engine_invoker: Optional[Any] = None,
        task_flag_resolver: Optional[Any] = None,
        current_image_vision_service: Optional[Any] = None,
        project_context_resolver: Optional[Any] = None,
        agent_runtime_ready: bool | None = None,
        agent_runtime_readiness_reason: str | None = None,
    ) -> None:
        self._session = session
        self._msg_repo = MessageRepository(session)
        self._conv_repo = ConversationRepository(session)
        self._file_repo = FileRepository(session)
        self._message_attachment_repo = MessageAttachmentRepository(session)
        self._task_file_binding_repo = TaskFileBindingRepository(session)
        self._task_repo = AgentTaskRepository(session)
        self._event_repo = EventRepository(session)
        self._exec_repo = AgentExecutionRequestRepository(session)
        self._llm = llm_client
        self._current_image_vision_service = current_image_vision_service
        self._context_llm_invoker = context_llm_invoker
        self._context_engine_invoker = context_engine_invoker
        # CE-05 WP-2: 任务级 Flag Resolver（冻结 Manifest）；None → 进程级。
        self._task_flag_resolver = task_flag_resolver
        # WP-BE-09: 普通 Chat 走 bridge 需要 session_factory（snapshot 落库）。
        # 用 AsyncSessionLocal 新开 session，避免与请求级 session 并发冲突。
        try:
            from app.db.session import AsyncSessionLocal as _SessionLocal

            self._session_factory = _SessionLocal
        except Exception:  # noqa: BLE001
            self._session_factory = None
        self._router = intent_router or (
            IntentRouter(
                llm_client,
                context_llm_invoker=context_llm_invoker,
                task_flag_resolver=task_flag_resolver,
                session_factory=self._session_factory,
            ) if llm_client is not None else None
        )
        self._chat = chat_service or (
            ChatLLMService(
                llm_client,
                context_llm_invoker=context_llm_invoker,
                context_engine_invoker=context_engine_invoker,
                task_flag_resolver=task_flag_resolver,
                session_factory=self._session_factory,
            ) if llm_client is not None else None
        )
        # F016: context services (lazy-initialized in send_message/stream_message)
        self._context_svc: ConversationContextService | None = None
        self._summary_svc: ConversationSummaryService | None = None
        self._snapshot_svc: ContextSnapshotService | None = None
        self._project_context_resolver = project_context_resolver
        self._agent_runtime_ready = agent_runtime_ready
        self._agent_runtime_readiness_reason = agent_runtime_readiness_reason
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _ensure_context_services(self) -> None:
        """Lazily create F016 context services from the shared session."""
        if self._context_svc is None:
            self._context_svc = ConversationContextService(self._session)
        if self._summary_svc is None:
            self._summary_svc = ConversationSummaryService(
                self._session, llm_client=self._llm,
                context_llm_invoker=self._context_llm_invoker,
                task_flag_resolver=self._task_flag_resolver,
                session_factory=self._session_factory,
            )
        if self._snapshot_svc is None:
            self._snapshot_svc = ContextSnapshotService(self._session)

    async def _resolve_project_context(
        self,
        *,
        conv,
        user_internal_id: int,
        query: str,
        task_id: int | str | None = None,
    ) -> dict[str, Any] | None:
        if getattr(conv, "project_id", None) is None:
            return None
        try:
            if self._project_context_resolver is None:
                from app.services.project_context_resolver import ProjectContextResolver

                self._project_context_resolver = ProjectContextResolver(self._session)
            package = await self._project_context_resolver.resolve(
                user_id=user_internal_id,
                conversation_id=conv.id,
                query=query,
                task_id=task_id,
            )
            return package if package.get("project_id") else None
        except Exception as exc:  # noqa: BLE001 - Project context is fail-open
            logger.warning(
                "MessageService project context degraded | conv=%s | err=%s",
                getattr(conv, "public_id", None),
                type(exc).__name__,
            )
            return None

    async def _resolve_project_workspace_key(
        self,
        *,
        conv,
        user_internal_id: int,
    ) -> str | None:
        """Resolve the bound project's retrieval workspace without loading its sources.

        Intent recognition runs before task creation.  It still performs a
        Context Engine retrieval, so it must use the project workspace for a
        project-bound conversation instead of silently falling back to the
        conversation workspace.
        """
        project_id = getattr(conv, "project_id", None)
        if project_id is None:
            return None
        try:
            from app.models.project import Project

            result = await self._session.execute(
                select(Project.context_workspace_key).where(
                    Project.id == project_id,
                    Project.user_id == user_internal_id,
                    Project.deleted_at.is_(None),
                )
            )
            workspace_key = result.scalar_one_or_none()
            return str(workspace_key).strip() or None if workspace_key else None
        except Exception as exc:  # noqa: BLE001 - routing remains fail-open
            logger.warning(
                "MessageService project workspace degraded | conv=%s | err=%s",
                getattr(conv, "public_id", None),
                type(exc).__name__,
            )
            return None

    async def _project_has_sources(
        self,
        *,
        conv,
        user_internal_id: int,
        intent_result: IntentResult,
    ) -> bool:
        """Return whether a document question has a current Project Source.

        This is deliberately narrower than the general document-QA capability:
        a Project conversation may use its stored sources, while a standalone
        conversation still requires a current-message attachment.
        """
        if (
            intent_result.intent != IntentType.DOCUMENT_QUESTION
            or getattr(conv, "project_id", None) is None
        ):
            return False
        try:
            from app.repositories.project_source_repository import ProjectSourceRepository

            sources = await ProjectSourceRepository(self._session).list_with_files(
                conv.project_id
            )
            return any(bool(source.is_current) for source, _uploaded in sources)
        except Exception as exc:  # noqa: BLE001 - preserve existing unsupported fallback
            logger.warning(
                "MessageService project source gate degraded | conv=%s | user=%s | err=%s",
                getattr(conv, "public_id", None),
                user_internal_id,
                type(exc).__name__,
            )
            return False

    @staticmethod
    def _build_existing_task_reply(task_summary: object | None) -> str:
        """Build reply text for EXISTING_TASK_ACTION route."""
        if task_summary is None:
            return "没有找到进行中的任务，请描述你的需求。"
        status = getattr(task_summary, "status", "unknown")
        if status == "waiting_user_confirm":
            return "好的，我继续处理。请确认上面的章节处理建议。"
        if status in ("running", "generating", "reviewing", "exporting"):
            return "任务正在执行中，请稍等片刻。"
        return "已收到你的请求，我继续处理。"

    # ── Public API ───────────────────────────────────────────────

    async def list_messages(
        self, user_internal_id: int, conv_public_id: str
    ) -> list[dict]:
        conv = await self._conv_repo.get_by_public_id(conv_public_id)
        if not conv:
            return []
        messages = await self._msg_repo.list_by_conversation(user_internal_id, conv.id)
        files = await self._file_repo.list_by_conversation(user_internal_id, conv.id)
        return [self._to_detail(m, conv_public_id, files) for m in messages]

    async def send_message(
        self,
        conv_public_id: str,
        content: str,
        attached_file_ids: list[str] | None,
        user_internal_id: int,
        knowledge_mode_snapshot: str = KnowledgeMode.AUTO.value,
    ) -> dict:
        # 1) Resolve conversation
        conv = await self._conv_repo.get_by_public_id(conv_public_id)
        if not conv:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("会话")

        now = utcnow()
        attached_ids = list(attached_file_ids or [])
        attached_id_set = set(attached_ids)
        knowledge_mode = normalize_knowledge_mode(knowledge_mode_snapshot)
        files = await self._file_repo.list_by_conversation(user_internal_id, conv.id)
        default_template_file_id = await self._maybe_attach_default_test_plan_template(
            conv=conv,
            user_internal_id=user_internal_id,
            content=content,
            files=files,
            attached_id_set=attached_id_set,
        )
        if default_template_file_id:
            attached_ids.append(default_template_file_id)
            attached_id_set.add(default_template_file_id)
            files = await self._file_repo.list_by_conversation(user_internal_id, conv.id)

        conversation_summary = await self._maybe_generate_conversation_title(
            conv.public_id,
            conv.title,
            content,
            user_internal_id,
            now,
            files=files,
            attached_id_set=attached_id_set,
        )

        # 2) Write the user message
        user_msg = Message(
            public_id=generate_public_id("message"),
            user_id=user_internal_id,
            conversation_id=conv.id,
            role="user",
            message_type="user_text",
            content=content,
            payload_json=self._user_message_payload(attached_ids, knowledge_mode),
            status="sent",
            created_at=now,
            updated_at=now,
        )
        user_msg = await self._msg_repo.create(user_msg)
        missing_attached_file_ids = await self._dual_write_message_attachments(
            message=user_msg,
            attached_ids=attached_ids,
            files=files,
        )

        # P0 收口:cache invalidation 必须严格在 commit 之后.
        try:
            from app.cache.domains.conversation_cache import (
                get_conversation_cache,
            )
            from app.db.sync import register_after_commit

            _uid = user_internal_id
            _conv_pid = conv_public_id

            async def _do_bump(_session):
                try:
                    await get_conversation_cache().bump_generation(_uid)
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "MessageService.send_message: bump_generation failed | "
                        "user_id=%s | conv=%s | %s",
                        _uid, _conv_pid, cache_exc,
                    )

            register_after_commit(self._session, _do_bump)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MessageService.send_message: cache hook register failed | "
                "user_id=%s | conv=%s | %s",
                user_internal_id, conv_public_id, exc,
            )

        # The Context Engine preflight may use a separate session to persist a
        # compaction run, and intent/chat generation can wait on an external
        # provider.  Do not retain this request transaction (and its user
        # message / attachment locks) across either operation; otherwise the
        # compactor can block on MySQL and fail with lock-wait timeout 1205.
        await self._session.commit()

        user_msg_dict = self._to_detail(user_msg, conv_public_id, files)
        if missing_attached_file_ids:
            intent_result = self._attached_files_not_found_intent(
                content, missing_attached_file_ids
            )
            result = await self._create_assistant_reply(
                conv, user_internal_id, content, now, user_msg_dict, intent_result, files,
                attached_id_set=attached_id_set,
                attached_ids=attached_ids,
                user_msg_internal_id=user_msg.id,
                knowledge_mode_snapshot=knowledge_mode,
            )
            if conversation_summary is not None:
                result["conversation"] = conversation_summary
            return result

        # 3a) F016: Build intent context for intent recognition
        self._ensure_context_services()
        intent_context: IntentContext | None = None
        chat_context: ChatContext | None = None
        try:
            intent_context = await self._context_svc.build_intent_context(
                user_id=user_internal_id,
                conversation_id=conv.id,
                current_message_id=user_msg.id,
                attached_file_ids=attached_ids,
            )
        except Exception as exc:
            logger.warning(
                "FALLBACK_USED | component=message_service.context_build | "
                "from=conversation_context | to=context_free_intent_routing | "
                "reason=context_build_exception | user_id=%s | conversation=%s | "
                "err_type=%s | err=%s",
                user_internal_id,
                conv.public_id,
                type(exc).__name__,
                str(exc)[:300],
            )

        project_workspace_key = await self._resolve_project_workspace_key(
            conv=conv,
            user_internal_id=user_internal_id,
        )

        # 4) Strict KB mode is an explicit user choice.  Treat it as a
        # backend routing fast-path instead of asking the intent LLM to infer
        # intent first; a classifier fallback must never suppress KB search.
        if knowledge_mode == KnowledgeMode.MAAS_STRICT:
            intent_result = self._company_rag_intent_result()
        else:
            intent_result = await self._recognize_intent(
                content, files, attached_ids, intent_context=intent_context,
                user_internal_id=user_internal_id,
                conversation_id=conv.id,
                conversation_public_id=conv.public_id,
                context_workspace_key=project_workspace_key,
            )
            intent_result = self._apply_file_requirement_override(
                intent_result, files, attached_id_set, content
            )
            project_source_available = await self._project_has_sources(
                conv=conv,
                user_internal_id=user_internal_id,
                intent_result=intent_result,
            )
            intent_result = self._apply_request_understanding_routing(
                content,
                intent_result,
                files=files,
                attached_ids=attached_ids,
                intent_context=intent_context,
                project_source_available=project_source_available,
            )

        logger.info(
            "MessageService.send_message: 路由决策 | route=%s | intent=%s | confidence=%.2f | reason=%s",
            intent_result.route.value, intent_result.intent.value,
            intent_result.confidence, intent_result.reason,
        )

        # 6) Dispatch by route
        if intent_result.route == MessageRoute.AGENT_TASK:
            logger.info(
                "MessageService.send_message: 分发到AgentTask | task_type=%s",
                intent_result.intent.value,
            )
            result = await self._create_agent_task(
                conv, user_internal_id, content, now, user_msg_dict, intent_result,
                files=files,
                attached_id_set=attached_id_set,
                attached_ids=attached_ids,
                user_msg_internal_id=user_msg.id,
                knowledge_mode_snapshot=knowledge_mode,
            )
        elif intent_result.route == MessageRoute.EXISTING_TASK_ACTION:
            result = await self._create_assistant_reply(
                conv, user_internal_id, content, now, user_msg_dict, intent_result, files,
                intent_context=intent_context,
                attached_id_set=attached_id_set,
                attached_ids=attached_ids,
                user_msg_internal_id=user_msg.id,
                knowledge_mode_snapshot=knowledge_mode,
            )
        else:
            # Build chat_context for chat_reply
            if intent_result.route == MessageRoute.CHAT_REPLY:
                try:
                    project_context = await self._resolve_project_context(
                        conv=conv,
                        user_internal_id=user_internal_id,
                        query=content,
                    )
                    chat_context = await self._context_svc.build_chat_context(
                        user_id=user_internal_id,
                        conversation_id=conv.id,
                        current_message_id=user_msg.id,
                        project_context=project_context,
                    )
                except Exception as exc:
                    logger.warning("MessageService: chat_context build failed: %s", exc)
            logger.debug(
                "MessageService.send_message: 生成助手回复 | route=%s",
                intent_result.route.value,
            )
            result = await self._create_assistant_reply(
                conv, user_internal_id, content, now, user_msg_dict, intent_result, files,
                chat_context=chat_context,
                attached_id_set=attached_id_set,
                attached_ids=attached_ids,
                user_msg_internal_id=user_msg.id,
                knowledge_mode_snapshot=knowledge_mode,
            )
        if conversation_summary is not None:
            result["conversation"] = conversation_summary

        # Summary and memory extraction can each invoke an external model.
        # Never keep the request-scoped database transaction open while those
        # calls are in flight: a provider/database network interruption could
        # otherwise leave row locks behind and block the account's next login.
        # The streaming path already follows this fresh-session maintenance
        # pattern; normal chat must have the same transaction boundary.
        self._schedule_post_chat_maintenance(
            conv=conv,
            user_message=content,
            user_internal_id=user_internal_id,
            intent_result=intent_result,
        )

        return result

    async def stream_message(
        self,
        conv_public_id: str,
        content: str,
        attached_file_ids: list[str] | None,
        user_internal_id: int,
        knowledge_mode_snapshot: str = KnowledgeMode.AUTO.value,
    ) -> AsyncIterator[dict]:
        conv = await self._conv_repo.get_by_public_id(conv_public_id)
        if not conv:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("浼氳瘽")

        now = utcnow()
        attached_ids = list(attached_file_ids or [])
        attached_id_set = set(attached_ids)
        knowledge_mode = normalize_knowledge_mode(knowledge_mode_snapshot)
        files = await self._file_repo.list_by_conversation(user_internal_id, conv.id)
        default_template_file_id = await self._maybe_attach_default_test_plan_template(
            conv=conv,
            user_internal_id=user_internal_id,
            content=content,
            files=files,
            attached_id_set=attached_id_set,
        )
        if default_template_file_id:
            attached_ids.append(default_template_file_id)
            attached_id_set.add(default_template_file_id)
            files = await self._file_repo.list_by_conversation(user_internal_id, conv.id)
        # Capture ORM attributes before spawning async task (session may
        # close before the task runs, causing DetachedInstanceError).
        conv_public_id_str = conv.public_id
        conv_title_str = conv.title
        # Title generation is optional and must never delay the first SSE event
        # or task creation. It uses its own session because AsyncSession cannot
        # be shared concurrently with the request transaction.
        title_task = self._schedule_conversation_title_update(
            conv_public_id=conv_public_id_str,
            conv_title=conv_title_str,
            content=content,
            user_internal_id=user_internal_id,
            now=now,
            files=files,
            attached_id_set=attached_id_set,
        )

        user_msg = Message(
            public_id=generate_public_id("message"),
            user_id=user_internal_id,
            conversation_id=conv.id,
            role="user",
            message_type="user_text",
            content=content,
            payload_json=self._user_message_payload(attached_ids, knowledge_mode),
            status="sent",
            created_at=now,
            updated_at=now,
        )
        user_msg = await self._msg_repo.create(user_msg)
        missing_attached_file_ids = await self._dual_write_message_attachments(
            message=user_msg,
            attached_ids=attached_ids,
            files=files,
        )

        # F016: Build intent context
        self._ensure_context_services()
        intent_context: IntentContext | None = None
        chat_context: ChatContext | None = None
        try:
            intent_context = await self._context_svc.build_intent_context(
                user_id=user_internal_id,
                conversation_id=conv.id,
                current_message_id=user_msg.id,
                attached_file_ids=attached_ids,
            )
        except Exception as exc:
            logger.warning(
                "FALLBACK_USED | component=message_service.context_build | "
                "from=conversation_context | to=context_free_intent_routing | "
                "reason=context_build_exception | user_id=%s | conversation=%s | "
                "err_type=%s | err=%s",
                user_internal_id,
                conv.public_id,
                type(exc).__name__,
                str(exc)[:300],
            )

        project_workspace_key = await self._resolve_project_workspace_key(
            conv=conv,
            user_internal_id=user_internal_id,
        )

        user_msg_dict = self._to_detail(user_msg, conv_public_id, files)

        conversation_summary = self._completed_title_summary(title_task)

        message_created = {"message": user_msg_dict}
        if conversation_summary is not None:
            message_created["conversation"] = conversation_summary
        yield {"event": "message_created", "data": message_created}

        if missing_attached_file_ids:
            intent_result = self._attached_files_not_found_intent(
                content, missing_attached_file_ids
            )
            async for event in self._stream_assistant_reply(
                conv,
                user_internal_id,
                content,
                now,
                user_msg_dict,
                intent_result,
                files,
                attached_id_set=attached_id_set,
                attached_ids=attached_ids,
                user_msg_internal_id=user_msg.id,
                knowledge_mode_snapshot=knowledge_mode,
            ):
                yield event
            return

        if knowledge_mode == KnowledgeMode.MAAS_STRICT:
            intent_result = self._company_rag_intent_result()
        else:
            intent_result = await self._recognize_intent(
                content, files, attached_ids, intent_context=intent_context,
                user_internal_id=user_internal_id,
                conversation_id=conv.id,
                conversation_public_id=conv.public_id,
                context_workspace_key=project_workspace_key,
            )
            intent_result = self._apply_file_requirement_override(
                intent_result, files, attached_id_set, content
            )
            project_source_available = await self._project_has_sources(
                conv=conv,
                user_internal_id=user_internal_id,
                intent_result=intent_result,
            )
            intent_result = self._apply_request_understanding_routing(
                content,
                intent_result,
                files=files,
                attached_ids=attached_ids,
                intent_context=intent_context,
                project_source_available=project_source_available,
            )

        if intent_result.route == MessageRoute.AGENT_TASK:
            result = await self._create_agent_task(
                conv, user_internal_id, content, now, user_msg_dict, intent_result,
                files=files,
                attached_id_set=attached_id_set,
                attached_ids=attached_ids,
                user_msg_internal_id=user_msg.id,
                knowledge_mode_snapshot=knowledge_mode,
            )
            conversation_summary = (
                conversation_summary or self._completed_title_summary(title_task)
            )
            if conversation_summary is not None:
                result["conversation"] = conversation_summary
            if result.get("agent_task") is not None:
                yield {"event": "agent_task_created", "data": result}
                return

            # Attachment-role resolution can downgrade an intended task to a
            # persisted explanatory assistant reply.  It must use the same
            # preview -> delta -> done lifecycle as every other chat reply.
            # Previously we mislabeled the whole send-result envelope as one
            # ``agent_reply_created`` event: its ``message`` field is the user
            # message, and no terminal event followed, so the frontend kept
            # the assistant placeholder on “正在思考...” forever.
            agent_reply = result.get("agent_reply")
            if isinstance(agent_reply, dict):
                agent_message_id = str(agent_reply.get("message_id") or "")
                reply_text = str(agent_reply.get("content") or "")
                yield {
                    "event": "agent_reply_created",
                    "data": {
                        "message": {**agent_reply, "content": ""},
                        "route": result.get("route"),
                        "intent": result.get("intent"),
                        "requires_sse": False,
                        "task_id": None,
                    },
                }
                if reply_text:
                    yield {
                        "event": "agent_text_delta",
                        "data": {
                            "message_id": agent_message_id,
                            "delta": reply_text,
                        },
                    }
                yield {"event": "agent_text_done", "data": result}
                return

            yield {
                "event": "error",
                "data": {"message": "附件用途确认失败，请重新选择文件后重试"},
            }
            return

        # Build chat_context for chat_reply
        if intent_result.route == MessageRoute.CHAT_REPLY:
            try:
                project_context = await self._resolve_project_context(
                    conv=conv,
                    user_internal_id=user_internal_id,
                    query=content,
                )
                chat_context = await self._context_svc.build_chat_context(
                    user_id=user_internal_id,
                    conversation_id=conv.id,
                    current_message_id=user_msg.id,
                    project_context=project_context,
                )
            except Exception as exc:
                logger.warning("MessageService: chat_context build failed: %s", exc)

        async for event in self._stream_assistant_reply(
            conv,
            user_internal_id,
            content,
            now,
            user_msg_dict,
            intent_result,
            files,
            chat_context=chat_context,
            intent_context=intent_context,
            attached_id_set=attached_id_set,
            attached_ids=attached_ids,
            user_msg_internal_id=user_msg.id,
            knowledge_mode_snapshot=knowledge_mode,
        ):
            yield event

        # ``agent_text_done`` is the user-visible terminal state.  Summary
        # and memory work must not keep the HTTP stream open after it.
        self._schedule_post_chat_maintenance(
            conv=conv,
            user_message=content,
            user_internal_id=user_internal_id,
            intent_result=intent_result,
        )

    def _schedule_post_chat_maintenance(
        self,
        *,
        conv,
        user_message: str,
        user_internal_id: int,
        intent_result,
    ) -> None:
        """Run non-user-visible maintenance only after SSE can close."""
        # Values must be read while the request session still owns ``conv``.
        # A background task runs after that session is committed and closed;
        # retaining an ORM instance here would cause DetachedInstanceError.
        conversation_internal_id = int(conv.id)
        conversation_public_id = str(conv.public_id)
        project_internal_id = conv.project_id
        active_summary_task = type(self)._summary_maintenance_tasks.get(conversation_internal_id)
        run_summary = active_summary_task is None or active_summary_task.done()

        async def _run() -> None:
            try:
                # Learning creates its own database session and schedules the
                # actual write independently.  Queue it before optional
                # summary work so a slow summary cannot postpone Memory.
                await self._maybe_context_learn(
                    conversation_public_id=conversation_public_id,
                    conversation_internal_id=conversation_internal_id,
                    project_internal_id=project_internal_id,
                    user_message=user_message,
                    user_internal_id=user_internal_id,
                )

                if run_summary:
                    # Never run post-SSE database work on the request session:
                    # FastAPI's dependency may concurrently commit/close it.
                    session_factory = self._session_factory
                    if session_factory is None:
                        logger.warning(
                            "MessageService: summary maintenance skipped; no session factory | conv=%s",
                            conversation_internal_id,
                        )
                    else:
                        async with session_factory() as summary_session:
                            summary_service = ConversationSummaryService(
                                summary_session,
                                llm_client=self._llm,
                                context_llm_invoker=self._context_llm_invoker,
                                task_flag_resolver=self._task_flag_resolver,
                                session_factory=session_factory,
                            )
                            await summary_service.maybe_update_summary(
                                conversation_internal_id,
                                user_internal_id,
                            )
                            await summary_session.commit()
                else:
                    logger.info(
                        "MessageService: summary maintenance coalesced | conv=%s",
                        conversation_internal_id,
                    )
            except Exception as exc:  # noqa: BLE001 - best-effort maintenance
                logger.warning(
                    "MessageService: post-chat maintenance failed | conv=%s | err_type=%s",
                    conversation_internal_id,
                    type(exc).__name__,
                )
            finally:
                logger.info(
                    "CHAT_SSE_SERVICE_FINALLY | conv=%d | intent=%s | route=%s",
                    conversation_internal_id,
                    getattr(intent_result, "intent", None),
                    getattr(intent_result, "route", None),
                )

        task = asyncio.create_task(
            _run(),
            name=f"post-chat-maintenance-{conversation_internal_id}",
        )
        self._background_tasks.add(task)

        if run_summary:
            type(self)._summary_maintenance_tasks[conversation_internal_id] = task

        def _on_done(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            if type(self)._summary_maintenance_tasks.get(conversation_internal_id) is completed:
                type(self)._summary_maintenance_tasks.pop(conversation_internal_id, None)

        task.add_done_callback(_on_done)

    async def _maybe_context_learn(
        self,
        *,
        conversation_public_id: str | None,
        conversation_internal_id: int | None,
        project_internal_id: int | None,
        user_message: str,
        user_internal_id: int,
    ) -> None:
        """WP-BE-06/08: Post-turn Context Learning（soft-async，绝不阻塞终态）。

        Receives scalar identifiers rather than a Conversation ORM object so
        it remains safe after the originating request session is closed.
        """
        # 只处理用户消息（禁止把 assistant 建议当 User Memory）。
        # 受 CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED flag 控制。
        try:
            from app.context_engine.feature_flags import (
                get_context_engine_flags,
                validate_full_chain_configuration,
            )

            flags = get_context_engine_flags()
            if not (
                flags.context_memory_auto_extract_enabled
                and flags.context_memory_write_enabled
            ):
                return
            if not (user_message or "").strip():
                return
            if flags.context_engine_enabled and self._context_llm_invoker is not None:
                invoker = self._context_llm_invoker
            else:
                invoker = None
            session_factory = getattr(self, "_session_factory", None)
            if session_factory is None:
                logger.debug("MessageService context learning skipped: no session factory")
                return

            async def learn_in_fresh_session() -> None:
                try:
                    from app.models.project import Project
                    from app.services.context_learning_service import ContextLearningService

                    async with session_factory() as learning_session:
                        project_public_id = None
                        project_memory_enabled = True
                        if project_internal_id is not None:
                            project = await learning_session.get(Project, project_internal_id)
                            if (
                                project is not None
                                and project.user_id == user_internal_id
                                and project.deleted_at is None
                            ):
                                project_public_id = project.public_id
                                project_memory_enabled = project.memory_mode != "off"
                        learning = ContextLearningService(
                            learning_session,
                            llm_invoker=invoker,
                            session_factory=session_factory,
                            llm_client=self._llm,
                        )
                        learning_result = await learning.learn_from_user_message(
                            user_message=user_message,
                            user_internal_id=user_internal_id,
                            conversation_public_id=conversation_public_id,
                            conversation_internal_id=conversation_internal_id,
                            allow_project_rule=True,
                            project_public_id=project_public_id,
                            project_memory_enabled=project_memory_enabled,
                        )
                        # Audit the asynchronous outcome without logging user
                        # content.  Previously a completed CE extraction that
                        # persisted zero items was indistinguishable from a
                        # background failure in normal INFO-level logs.
                        logger.info(
                            "CONTEXT_MEMORY_LEARNING_COMPLETED | conv=%s | persisted=%s | skipped=%s | error=%s",
                            conversation_internal_id,
                            learning_result.get("persisted", 0),
                            learning_result.get("skipped"),
                            learning_result.get("error"),
                        )
                except Exception as exc:  # noqa: BLE001 — background learning is fail-open
                    logger.warning(
                        "MessageService background context learning failed | conv=%s | err=%s",
                        conversation_internal_id,
                        type(exc).__name__,
                    )

            asyncio.create_task(
                learn_in_fresh_session(),
                name=f"context-learning-{conversation_internal_id or 'unknown'}",
            )
        except Exception as exc:  # noqa: BLE001 — soft-async，失败只记录
            logger.debug(
                "MessageService context learning failed | conv=%d | err=%s",
                conversation_internal_id or 0, type(exc).__name__,
            )

    async def _maybe_generate_conversation_title(
        self,
        conv_public_id: str,
        conv_title: str | None,
        content: str,
        user_internal_id: int,
        now,
        *,
        files: list[UploadedFile] | None = None,
        attached_id_set: set[str] | None = None,
    ) -> dict | None:
        if not self._should_generate_conversation_title(conv_title):
            return None
        title = await self._generate_conversation_title(
            content,
            files=files,
            attached_id_set=attached_id_set,
            user_id=user_internal_id,
        )
        if not title:
            return None
        await self._conv_repo.update_title(conv_public_id, title, now)
        return {
            "id": conv_public_id,
            "title": title,
            "updated_at": now.isoformat() if now else "",
        }

    def _schedule_conversation_title_update(
        self,
        *,
        conv_public_id: str,
        conv_title: str | None,
        content: str,
        user_internal_id: int,
        now,
        files: list[UploadedFile] | None = None,
        attached_id_set: set[str] | None = None,
    ) -> asyncio.Task[dict | None]:
        """Persist an optional title without sharing the request session."""

        async def _run() -> dict | None:
            if not self._should_generate_conversation_title(conv_title):
                return None
            title = await self._generate_conversation_title(
                content,
                files=files,
                attached_id_set=attached_id_set,
                user_id=user_internal_id,
            )
            if not title:
                return None

            session_factory = getattr(self, "_session_factory", None)
            if session_factory is None:
                # Isolated unit services may intentionally omit a factory.
                await self._conv_repo.update_title(conv_public_id, title, now)
            else:
                async with session_factory() as title_session:
                    await ConversationRepository(title_session).update_title(
                        conv_public_id, title, now
                    )
                    await title_session.commit()
            return {
                "id": conv_public_id,
                "title": title,
                "updated_at": now.isoformat() if now else "",
            }

        task = asyncio.create_task(_run())
        self._background_tasks.add(task)

        def _finish(done: asyncio.Task[dict | None]) -> None:
            self._background_tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MessageService: background title update failed | "
                    "conversation=%s | err_type=%s | err=%s",
                    conv_public_id,
                    type(exc).__name__,
                    str(exc)[:300],
                )

        task.add_done_callback(_finish)
        return task

    @staticmethod
    def _completed_title_summary(
        task: asyncio.Task[dict | None],
    ) -> dict | None:
        if not task.done() or task.cancelled():
            return None
        try:
            return task.result()
        except Exception:  # handled and logged by the task callback
            return None

    def _should_generate_conversation_title(self, title: str | None) -> bool:
        normalized = (title or "").strip()
        return not normalized or normalized in DEFAULT_CONVERSATION_TITLES

    async def _generate_conversation_title(
        self,
        content: str,
        *,
        files: list[UploadedFile] | None = None,
        attached_id_set: set[str] | None = None,
        user_id: int | None = None,
    ) -> str | None:
        """Generate a short conversation title via ``TITLE_PROFILE``.

        F014 closeout: routes through ``LLMClient.generate_with_profile``
        so the contract layer owns parsing (``PlainTextParser``) and
        fallback policy (``TITLE_PROFILE.fallback_text``).  The
        previous direct ``generate_with_system`` call is gone — the
        title generator now exercises the same contract surface as
        chat replies and intent recognition.

        CE-04：MIG_SUMMARY=true 时走 ContextInvokerBridge；bridge 缺失/
        失败返回 ``None``（标题生成失败不阻塞消息）。
        """
        if self._llm is None:
            return None
        file_based_title = self._build_file_based_conversation_title(
            content,
            files or [],
            attached_id_set or set(),
        )
        # Strip URLs from content to prevent them appearing in titles
        import re as _re
        clean_content = _re.sub(r'https?://\S+', '', content).strip()
        if not clean_content:
            clean_content = content[:50]
        title_input = self._build_title_generation_input(
            clean_content,
            files or [],
            file_based_title,
        )
        try:
            from app.context_engine.feature_flags import get_context_engine_flags

            # CE-05 WP-2：任务路径经 task_flag_resolver 读 MIG_SUMMARY（冻结 Manifest）
            mig_summary = True
            if mig_summary:
                # CE-04：MIG_SUMMARY=true → 只走 ContextInvokerBridge；
                # 不可用 → None（标题失败不阻塞消息，不静默回退 legacy）。
                bridge = self._context_llm_invoker
                if bridge is None or not getattr(bridge, "available", False):
                    logger.warning(
                        "MessageService: MIG_SUMMARY=true 但 Invoker 不可用",
                    )
                    logger.warning(
                        "FALLBACK_USED | component=message_service.title_generation | "
                        "from=context_bridge_title | to=no_title_update | "
                        "reason=bridge_unavailable | user_id=%s",
                        user_id,
                    )
                    return None
                bres = await bridge.generate(
                    user_id=user_id or 0,
                    call_site="conversation.title",
                    llm_task_profile=TITLE_PROFILE,
                    current_goal=title_input,
                    output_contract="text",
                    user_content=title_input,
                    # WP-BE-09 fix: bridge 路径需要 runtime_context（adapter
                    # 经 session_factory 读 CONVERSATION/CURRENT_GOAL）。
                    # 之前传 None → adapter 抛 AttributeError → 标题生成失败。
                    runtime_context=self._build_title_runtime_context(user_id),
                )
                if bres is None or bres.value is None:
                    logger.warning(
                        "MessageService: MIG_SUMMARY bridge 返回空",
                    )
                    logger.warning(
                        "FALLBACK_USED | component=message_service.title_generation | "
                        "from=context_bridge_title | to=no_title_update | "
                        "reason=bridge_value_none | user_id=%s",
                        user_id,
                    )
                    return None
                title = self._sanitize_conversation_title(bres.value)
                if file_based_title and self._should_prefer_file_based_title(
                    title, file_based_title
                ):
                    return file_based_title
                return title
            from app.integrations.llm_client import LLMProfileResult

            return file_based_title
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=message_service.title_generation | "
                "from=llm_title | to=file_based_or_no_title_update | "
                "reason=title_generation_exception | user_id=%s | err_type=%s | err=%s",
                user_id,
                type(exc).__name__,
                str(exc)[:300],
            )
            return file_based_title
        title = self._sanitize_conversation_title(result)
        if file_based_title and self._should_prefer_file_based_title(title, file_based_title):
            return file_based_title
        return title

    def _build_title_runtime_context(self, user_id: int | None):
        """为标题生成构造轻量 runtime_context（bridge 路径需要 session_factory）。

        与 IntentRouter._build_runtime_context / ChatLLMService._build_runtime_context
        同语义：提供 ``session_factory`` 满足 SourceAdapter 读 session（如
        CURRENT_GOAL / CONVERSATION），避免 ``NoneType.session_factory`` 报错。
        """
        if self._session_factory is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            session_factory=self._session_factory,
            llm_client=self._llm,
            user_internal_id=int(user_id or 0),
        )

    @classmethod
    def _build_file_based_conversation_title(
        cls,
        content: str,
        files: list[UploadedFile],
        attached_id_set: set[str],
    ) -> str | None:
        if not cls._is_explicit_test_plan_request(content):
            return None

        requirement_files = [
            file
            for file in files
            if getattr(file, "file_type", "") == "requirement_doc"
        ]
        if attached_id_set:
            attached_requirement_files = [
                file
                for file in requirement_files
                if getattr(file, "public_id", "") in attached_id_set
            ]
            requirement_files = attached_requirement_files or requirement_files
        if not requirement_files:
            return None

        project_name = cls._extract_project_name_from_filename(
            getattr(requirement_files[0], "original_name", "")
        )
        if not project_name:
            return None
        return cls._compose_test_plan_title(project_name)

    @staticmethod
    def _build_title_generation_input(
        clean_content: str,
        files: list[UploadedFile],
        file_based_title: str | None,
    ) -> str:
        file_lines = [
            f"- {getattr(file, 'original_name', '')}（{getattr(file, 'file_type', '')}）"
            for file in files
            if getattr(file, "original_name", "")
        ]
        parts = [f"用户第一句话：{clean_content}"]
        if file_lines:
            parts.append("已上传文件：\n" + "\n".join(file_lines))
        if file_based_title:
            parts.append(f"推荐标题：{file_based_title}")
        return "\n".join(parts)

    @staticmethod
    def _extract_project_name_from_filename(filename: str) -> str:
        stem = Path(filename or "").stem.strip()
        if not stem:
            return ""

        stem = re.sub(r"^\s*\d+[\s._-]*", "", stem)
        stem = re.sub(r"[\s._-]+", "_", stem)
        known_project_names = ("智慧校园",)
        for project_name in known_project_names:
            if project_name in stem:
                return project_name

        stem = re.sub(
            r"(?i)[_\s-]*(prd|产品需求文档|需求说明书|需求文档|需求规格说明书|需求规格|需求|说明书|原型文档).*$",
            "",
            stem,
        )
        stem = stem.strip("_ -")
        if "_" in stem:
            stem = stem.split("_", 1)[0].strip()
        return stem.strip()

    @staticmethod
    def _compose_test_plan_title(project_name: str) -> str:
        prefix = "生成"
        suffix = "测试方案"
        max_title_len = 18
        max_project_len = max_title_len - len(prefix) - len(suffix)
        compact_project = project_name.strip()[:max_project_len]
        return f"{prefix}{compact_project}{suffix}"

    @staticmethod
    def _should_prefer_file_based_title(generated_title: str | None, file_based_title: str) -> bool:
        if not generated_title:
            return True
        bad_prefixes = ("用户", "帮我", "根据这份", "这份", "请")
        if generated_title.startswith(bad_prefixes):
            return True
        if "测试方案" not in generated_title:
            return True
        project_name = file_based_title.removeprefix("生成").removesuffix("测试方案")
        return bool(project_name and project_name not in generated_title)

    @staticmethod
    def _sanitize_conversation_title(result: object) -> str | None:
        """Normalise a contract-layer result into a short title (<=12 chars).

        Strips URLs, quotes, meta-text prefixes, and truncates.
        """
        try:
            from app.integrations.llm_client import LLMProfileResult
        except ImportError:
            LLMProfileResult = None  # type: ignore[assignment]

        parsed: object
        success: bool
        if LLMProfileResult is not None and isinstance(result, LLMProfileResult):
            success = bool(getattr(result, "success", False))
            parsed = getattr(result, "parsed", None)
        else:
            success = True
            parsed = result

        if not success or not isinstance(parsed, str):
            logger.warning(
                "FALLBACK_USED | component=message_service.title_generation | "
                "from=llm_title | to=default_title | "
                "reason=invalid_profile_result | success=%s | parsed_type=%s",
                success,
                type(parsed).__name__,
            )
            return TITLE_PROFILE.fallback_text
        text = parsed.strip()
        if not text:
            logger.warning(
                "FALLBACK_USED | component=message_service.title_generation | "
                "from=llm_title | to=default_title | reason=empty_title"
            )
            return TITLE_PROFILE.fallback_text

        # Post-process: strip URLs, quotes, leading/trailing punctuation
        import re as _re
        text = _re.sub(r'https?://\S+', '', text)
        text = text.strip('\"\'""''\n\r\t ')

        # Take first line / first sentence only (LLM may output reasoning)
        for sep in ['\n', '。', '；', '，根据', '，因为', '，所以']:
            idx = text.find(sep)
            if 0 < idx < 20:
                text = text[:idx]
                break

        # Strip common meta-prefixes
        text = _re.sub(r'^(用户(?:输入|问题|提问|问|说的?)是?[：:]?|标题[：:]?|回复[：:]?)', '', text).strip()

        # Hard truncate to 12 chars
        if len(text) > 12:
            text = text[:12]

        if text:
            return text
        logger.warning(
            "FALLBACK_USED | component=message_service.title_generation | "
            "from=llm_title | to=default_title | reason=postprocess_empty_title"
        )
        return TITLE_PROFILE.fallback_text

    @staticmethod
    def _conversation_summary(conv) -> dict:
        return {
            "id": conv.public_id,
            "title": conv.title,
            "state": conv.status,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else "",
        }

    # ── Intent recognition ───────────────────────────────────────

    @staticmethod
    def _user_message_payload(
        attached_ids: list[str] | None,
        knowledge_mode_snapshot: KnowledgeMode | str | None = KnowledgeMode.AUTO,
    ) -> dict:
        return {
            "attached_file_ids": list(attached_ids or []),
            "knowledge_mode_snapshot": normalize_knowledge_mode(
                knowledge_mode_snapshot
            ).value,
        }

    @staticmethod
    def _task_context_with_knowledge_mode(
        task_context: dict,
        knowledge_mode_snapshot: KnowledgeMode | str | None = KnowledgeMode.AUTO,
    ) -> dict:
        payload = normalize_json_object(task_context)
        payload["knowledge_mode_snapshot"] = normalize_knowledge_mode(
            knowledge_mode_snapshot
        ).value
        return payload

    @staticmethod
    def _company_rag_intent_result() -> IntentResult:
        return IntentResult(
            intent=IntentType.KNOWLEDGE_QUESTION,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=1.0,
            reason="knowledge_mode_strict_fast_path",
            extra_payload={"knowledge_mode_fast_path": True},
        )

    def _apply_request_understanding_routing(
        self,
        content: str,
        intent_result: IntentResult,
        *,
        files: list[UploadedFile],
        attached_ids: list[str],
        intent_context: IntentContext | None = None,
        project_source_available: bool = False,
    ) -> IntentResult:
        if (
            intent_result.route == MessageRoute.UNSUPPORTED
            and intent_result.intent
            not in {
                IntentType.DOCUMENT_QUESTION,
                IntentType.KNOWLEDGE_QUESTION,
                IntentType.TEST_CASE_GENERATION,
            }
        ):
            return intent_result
        if (
            intent_result.route == MessageRoute.UNSUPPORTED
            and intent_result.intent == IntentType.TEST_CASE_GENERATION
            and not any(
                word in (content or "").lower()
                for word in ("生成", "编写", "创建", "输出", "generate", "create", "write")
            )
        ):
            return intent_result
        if (
            intent_result.route == MessageRoute.CHAT_REPLY
            and self._is_explicit_memory_rule_statement(content)
        ):
            logger.info(
                "REQUEST_UNDERSTANDING_BYPASSED | reason=explicit_memory_rule_statement"
            )
            return intent_result
        understanding = build_request_understanding(
            content,
            intent_result=intent_result,
            files=files,
            attached_file_ids=attached_ids,
            intent_context=intent_context,
        )
        decision = CapabilityRouter(CapabilityRegistry.default(maas_enabled=True)).route(
            understanding
        )
        if (
            understanding.target_capability == "document_qa"
            and not attached_ids
            and not project_source_available
        ):
            logger.info(
                "REQUEST_UNDERSTANDING_BYPASSED | "
                "reason=document_qa_requires_attachment_or_project_source"
            )
            return intent_result
        if (
            intent_result.route == MessageRoute.CLARIFY
            and understanding.target_capability == "general_chat"
        ):
            if understanding.degraded:
                logger.warning(
                    "FALLBACK_USED | component=message_service.request_understanding | "
                    "from=request_understanding | to=legacy_clarify | reason=%s | "
                    "route=%s | intent=%s",
                    understanding.reason,
                    intent_result.route.value,
                    intent_result.intent.value,
                )
            return intent_result
        target_intent = self._intent_for_capability(
            understanding.target_capability,
            fallback=intent_result.intent,
        )
        extra_payload = dict(getattr(intent_result, "extra_payload", {}) or {})
        extra_payload["request_understanding"] = understanding.model_dump(
            mode="python"
        )
        extra_payload["capability_routing"] = decision.model_dump(
            mode="python", exclude={"capability"}
        )
        if (
            decision.legacy_route != intent_result.route
            or target_intent != intent_result.intent
        ):
            logger.info(
                "REQUEST_UNDERSTANDING_AUDIT | component=message_service.request_understanding | "
                "llm_route=%s | capability_route=%s | llm_intent=%s | capability_intent=%s | "
                "target_capability=%s | execution_mode=%s | supported=%s | reason=%s",
                intent_result.route.value,
                decision.legacy_route.value,
                intent_result.intent.value,
                target_intent.value,
                understanding.target_capability,
                decision.execution_mode.value,
                decision.supported,
                decision.reason or understanding.reason,
            )
        if not decision.supported:
            logger.warning(
                "FALLBACK_USED | component=message_service.request_understanding | "
                "from=capability_router | to=%s | reason=%s | target_capability=%s",
                decision.legacy_route.value,
                decision.reason or understanding.reason,
                understanding.target_capability,
            )
        # RESULT_MODIFICATION 是「在已有 task 上做增量修改」—— CapabilityRouter 没有
        # 上下文判断是不是 incremental,容易把它误分类成 test_plan_generation。
        # 增量任务需要的是上一轮的 artifact(已经在 task_state 里),不是新一轮文件。
        # 这里直接保留原 intent + 走 existing_task_action 路由,避免被 capability router 误覆盖。
        if intent_result.intent == IntentType.RESULT_MODIFICATION:
            extra_payload["request_understanding"] = understanding.model_dump(
                mode="python"
            )
            extra_payload["capability_routing"] = decision.model_dump(
                mode="python", exclude={"capability"}
            )
            return intent_result.model_copy(
                update={"extra_payload": extra_payload}
            )

        # IntentRouter is the sole intent authority.  Request understanding
        # and capability routing enrich the audit record, but must not replace
        # the LLM's route with keyword-derived routing.
        return intent_result.model_copy(update={"extra_payload": extra_payload})

    @staticmethod
    def _intent_for_capability(
        capability_key: str,
        *,
        fallback: IntentType,
    ) -> IntentType:
        if capability_key == "document_qa":
            return IntentType.DOCUMENT_QUESTION
        if capability_key == "enterprise_knowledge_qa":
            return IntentType.KNOWLEDGE_QUESTION
        if capability_key == "test_plan_generation":
            return IntentType.TEST_PLAN_GENERATION
        if capability_key == "test_plan_incremental":
            return IntentType.RESULT_MODIFICATION
        if capability_key == "test_case_generation":
            return IntentType.TEST_CASE_GENERATION
        if capability_key in {"general_chat", "image_qa", "active_task_action"}:
            return IntentType.GENERAL_CHAT
        return fallback

    async def _recognize_intent(
        self,
        content: str,
        files: list[UploadedFile],
        attached_ids: list[str],
        *,
        intent_context: IntentContext | None = None,
        user_internal_id: int | None = None,
        conversation_id: int | None = None,
        conversation_public_id: str | None = None,
        context_workspace_key: str | None = None,
    ) -> IntentResult:
        if self._router is None:
            logger.warning(
                "FALLBACK_USED | component=message_service.intent_router | "
                "from=intent_router | to=clarify_reply | reason=router_not_configured"
            )
            return IntentResult(
                intent=IntentType.UNKNOWN,
                route=MessageRoute.CLARIFY,
                supported=False,
                confidence=0.0,
                reason="router_not_configured",
                reply_message=DEFAULT_CLARIFY_TEXT,
            )
        try:
            kwargs = {
                "attached_file_ids": attached_ids,
                "intent_context": intent_context,
                "user_internal_id": user_internal_id,
            }
            try:
                sig = inspect.signature(self._router.recognize)
                if "conversation_id" in sig.parameters:
                    kwargs["conversation_id"] = conversation_id
                elif conversation_id is not None:
                    logger.warning(
                        "FALLBACK_USED | component=message_service.intent_router | "
                        "from=conversation_aware_router_args | to=legacy_router_args | "
                        "reason=router_does_not_accept_conversation_id | user_id=%s | conv=%s",
                        user_internal_id,
                        conversation_id,
                    )
                if "conversation_public_id" in sig.parameters:
                    kwargs["conversation_public_id"] = conversation_public_id
                elif conversation_public_id is not None:
                    logger.warning(
                        "FALLBACK_USED | component=message_service.intent_router | "
                        "from=conversation_aware_router_args | to=legacy_router_args | "
                        "reason=router_does_not_accept_conversation_public_id | user_id=%s | conv=%s",
                        user_internal_id,
                        conversation_public_id,
                    )
                if "context_workspace_key" in sig.parameters:
                    kwargs["context_workspace_key"] = context_workspace_key
                elif context_workspace_key is not None:
                    logger.warning(
                        "FALLBACK_USED | component=message_service.intent_router | "
                        "from=project_workspace_router_arg | to=legacy_router_args | "
                        "reason=router_does_not_accept_context_workspace_key | user_id=%s | conv=%s",
                        user_internal_id,
                        conversation_id,
                    )
            except (TypeError, ValueError):
                kwargs["conversation_id"] = conversation_id
                kwargs["conversation_public_id"] = conversation_public_id
                kwargs["context_workspace_key"] = context_workspace_key
            return await self._router.recognize(content, files, **kwargs)
        except Exception as exc:  # noqa: BLE001 — belt-and-braces
            logger.exception(
                "FALLBACK_USED | component=message_service.intent_router | "
                "from=intent_router | to=clarify_reply | reason=router_outer_failure | "
                "err_type=%s | err=%s",
                type(exc).__name__,
                str(exc)[:300],
            )
            return IntentResult(
                intent=IntentType.UNKNOWN,
                route=MessageRoute.CLARIFY,
                supported=False,
                confidence=0.0,
                reason=f"router_outer_failure: {exc.__class__.__name__}",
                reply_message=DEFAULT_CLARIFY_TEXT,
            )

    async def _maybe_attach_default_test_plan_template(
        self,
        *,
        conv,
        user_internal_id: int,
        content: str,
        files: list[UploadedFile],
        attached_id_set: set[str],
    ) -> str | None:
        """Attach the configured template only when the request has a requirement.

        The copied template becomes a regular conversation attachment, so the
        existing attachment resolver, parser, exporter, and task bindings keep
        one consistent file contract.  User-selected templates always win.
        """
        if not self._is_explicit_test_plan_request(content):
            return None
        present = FileRequirementChecker.present_types(files, attached_id_set)
        if "requirement_doc" not in present or "test_plan_template" in present:
            return None

        template_name = get_settings().default_test_plan_template_name.strip()
        if not template_name:
            return None
        for file in files:
            if (
                getattr(file, "original_name", "") == f"{template_name}.docx"
                and getattr(file, "public_id", None)
            ):
                return str(file.public_id)

        try:
            from app.services.template_use_service import TemplateUseService

            materialized = await TemplateUseService(self._session).use_public_default_test_plan_template(
                user_id=user_internal_id,
                conversation_public_id=conv.public_id,
                template_name=template_name,
            )
        except (TemplateNotFoundError, TemplateVersionMissingError, TemplateUseCopyFailedError) as exc:
            logger.warning(
                "DEFAULT_TEST_PLAN_TEMPLATE_UNAVAILABLE | user_id=%s | conversation=%s | "
                "template_name=%s | error=%s",
                user_internal_id,
                conv.public_id,
                template_name,
                type(exc).__name__,
            )
            return None
        except Exception as exc:  # default selection must not break message persistence
            logger.exception(
                "DEFAULT_TEST_PLAN_TEMPLATE_UNEXPECTED_FAILURE | user_id=%s | conversation=%s | "
                "template_name=%s | error=%s",
                user_internal_id,
                conv.public_id,
                template_name,
                type(exc).__name__,
            )
            return None

        uploaded = materialized.get("uploaded_file") if isinstance(materialized, dict) else None
        file_id = str((uploaded or {}).get("id") or "").strip()
        if not file_id:
            logger.warning(
                "DEFAULT_TEST_PLAN_TEMPLATE_UNAVAILABLE | user_id=%s | conversation=%s | "
                "template_name=%s | reason=materialization_missing_file_id",
                user_internal_id,
                conv.public_id,
                template_name,
            )
            return None
        logger.info(
            "DEFAULT_TEST_PLAN_TEMPLATE_APPLIED | user_id=%s | conversation=%s | "
            "template_name=%s | file_id=%s",
            user_internal_id,
            conv.public_id,
            template_name,
            file_id,
        )
        return file_id

    def _apply_file_requirement_override(
        self,
        intent_result: IntentResult,
        files: list[UploadedFile],
        attached_id_set: set[str],
        content: str = "",
    ) -> IntentResult:
        """Override the LLM's route decision based on actual file state.

        Rules (only applied when intent == TEST_PLAN_GENERATION):
          - LLM said agent_task  &  files incomplete → ask_for_files
          - LLM said ask_for_files &  files complete   → agent_task
        """
        has_all = FileRequirementChecker.has_all_for_test_plan(
            files, attached_id_set
        )
        if (
            intent_result.route == MessageRoute.AGENT_TASK
            and intent_result.intent != IntentType.TEST_PLAN_GENERATION
            and intent_result.intent != IntentType.RESULT_MODIFICATION
        ):
            logger.warning(
                "MessageService: rejecting inconsistent agent_task route "
                "(intent=%s)",
                intent_result.intent.value,
            )
            if intent_result.intent == IntentType.GENERAL_CHAT:
                return intent_result.model_copy(
                    update={
                        "route": MessageRoute.CHAT_REPLY,
                        "need_files": False,
                        "required_file_types": [],
                        "missing_file_types": [],
                        "reply_message": None,
                    }
                )
            if intent_result.intent in INTENT_DEFAULT_REPLIES:
                return intent_result.model_copy(
                    update={
                        "route": MessageRoute.UNSUPPORTED,
                        "need_files": False,
                        "required_file_types": [],
                        "missing_file_types": [],
                        "reply_message": None,
                    }
                )
            return intent_result.model_copy(
                update={
                    "route": MessageRoute.CLARIFY,
                    "need_files": False,
                    "required_file_types": [],
                    "missing_file_types": [],
                    "reply_message": DEFAULT_CLARIFY_TEXT,
                }
            )

        if intent_result.intent != IntentType.TEST_PLAN_GENERATION:
            return intent_result

        # RESULT_MODIFICATION 走增量任务路径,需要的是已有 artifact(从 task_state 拿),
        # 不是新一轮上传。文件检查不应该把已有 task 的增量请求降级到 ask_for_files,
        # 否则前端会看到「请先上传文档」——这是错的(它会以为要重新生成)。
        if intent_result.intent == IntentType.RESULT_MODIFICATION:
            return intent_result

        if intent_result.route == MessageRoute.AGENT_TASK and not has_all:
            present = FileRequirementChecker.present_types(files, attached_id_set)
            missing = sorted(
                set(FileRequirementChecker.REQUIRED_FOR_TEST_PLAN) - present
            )
            logger.info(
                "MessageService: downgrading route agent_task -> ask_for_files "
                "(missing=%s)", missing,
            )
            return intent_result.model_copy(
                update={
                    "route": MessageRoute.ASK_FOR_FILES,
                    "need_files": True,
                    "required_file_types": list(
                        FileRequirementChecker.REQUIRED_FOR_TEST_PLAN
                    ),
                    "missing_file_types": missing,
                    "reply_message": DEFAULT_ASK_FOR_FILES_TEXT,
                }
            )
        if intent_result.route == MessageRoute.ASK_FOR_FILES and has_all:
            logger.info(
                "MessageService: upgrading route ask_for_files -> agent_task "
                "(files complete)",
            )
            return intent_result.model_copy(
                update={"route": MessageRoute.AGENT_TASK, "reply_message": None}
            )
        return intent_result

    @staticmethod
    def _missing_attached_file_ids(
        files: list[UploadedFile],
        attached_ids: list[str],
    ) -> list[str]:
        if not attached_ids:
            return []
        existing = {str(getattr(file, "public_id", "")) for file in files}
        return [
            file_public_id
            for file_public_id in attached_ids
            if file_public_id not in existing
        ]

    @staticmethod
    def _attached_files_not_found_intent(
        content: str,
        missing_attached_file_ids: list[str],
    ) -> IntentResult:
        missing_text = "、".join(missing_attached_file_ids)
        is_test_plan = MessageService._is_explicit_test_plan_request(content)
        next_action = "我会继续生成测试方案" if is_test_plan else "我会继续处理你的请求"
        reply_message = (
            "我收到了这次请求，但以下附件没有成功保存或不属于当前会话："
            f"{missing_text}。请重新上传这些文件后再发送，{next_action}。"
        )
        return IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION if is_test_plan else IntentType.GENERAL_CHAT,
            route=MessageRoute.ASK_FOR_FILES,
            supported=True,
            confidence=0.95,
            reason="attached_files_not_found",
            need_files=True,
            required_file_types=list(FileRequirementChecker.REQUIRED_FOR_TEST_PLAN)
            if is_test_plan
            else [],
            missing_file_types=[],
            reply_message=reply_message,
            extra_payload={"missing_attached_file_ids": missing_attached_file_ids},
        )

    @staticmethod
    def _incremental_unavailable_reply(
        user_msg_dict: dict,
        intent_result: IntentResult,
        reason: str,
    ) -> dict:
        reason_text = {
            "feature_disabled": "增量生成 Agent 当前未开启。",
            "source_artifact_missing": "当前会话还没有可用于增量修改的测试方案产物。",
            "source_state_missing": "最近的测试方案产物缺少可复用的结构化内容快照。",
        }.get(reason, "当前无法创建增量修改任务。")
        return {
            "message": user_msg_dict,
            "agent_task": None,
            "agent_reply": {
                "content": (
                    f"{reason_text} 请先完成一次测试方案生成后，再提出增量修改要求。"
                ),
                "payload": {"incremental_unavailable_reason": reason},
            },
            "route": MessageRoute.CHAT_REPLY.value,
            "intent": intent_result.intent.value,
            "requires_sse": False,
        }

    @staticmethod
    def _normalize_section_match_text(value: str) -> str:
        text = str(value or "").lower()
        text = re.sub(r"^\s*[\d一二三四五六七八九十]+[、.．\s]+", "", text)
        return re.sub(r"[\s\"'“”‘’《》<>（）()\[\]【】:：,，.。、_\-]+", "", text)

    @staticmethod
    def _section_title_aliases(title: str) -> set[str]:
        title = str(title or "").strip()
        normalized = MessageService._normalize_section_match_text(title)
        aliases = {normalized} if normalized else set()
        no_prefix = re.sub(r"^\s*\d+\s*", "", title).strip()
        no_prefix_norm = MessageService._normalize_section_match_text(no_prefix)
        if no_prefix_norm:
            aliases.add(no_prefix_norm)
        return aliases

    @staticmethod
    def _infer_incremental_scope_kind(content: str) -> str:
        text = content or ""
        if any(keyword in text for keyword in ("重新导出", "再导出", "导出一份", "re-export")):
            return "re_export"
        if any(keyword in text for keyword in ("复审", "重新审查", "再检查", "re-review")):
            return "re_review"
        if any(keyword in text for keyword in ("表格", "表头", "列", "table")):
            return "adjust_table"
        if any(keyword in text for keyword in ("新增", "补充", "追加", "增加", "extend")):
            return "extend_scope"
        return "modify_section"

    @staticmethod
    def _infer_incremental_target_section_ids(
        content: str,
        *,
        test_plan_content: dict,
        template_structure: dict,
    ) -> list[str]:
        query = MessageService._normalize_section_match_text(content)
        if not query:
            return []

        candidates: list[tuple[str, str]] = []

        def _add_candidate(section_id: object, title: object) -> None:
            sid = str(section_id or "").strip()
            if not sid:
                return
            for alias in MessageService._section_title_aliases(str(title or sid)):
                if alias:
                    candidates.append((sid, alias))

        section_package = normalize_json_object(
            test_plan_content.get("section_package") or test_plan_content
        )
        generated_sections = section_package.get("generated_sections")
        if isinstance(generated_sections, list):
            for section in generated_sections:
                section_obj = normalize_json_object(section)
                sid = (
                    section_obj.get("section_id")
                    or section_obj.get("id")
                    or section_obj.get("field")
                    or section_obj.get("field_name")
                )
                title = (
                    section_obj.get("title")
                    or section_obj.get("section_title")
                    or section_obj.get("heading")
                    or sid
                )
                _add_candidate(sid, title)
        elif isinstance(generated_sections, dict):
            for sid, section in generated_sections.items():
                section_obj = normalize_json_object(section)
                title = (
                    section_obj.get("title")
                    or section_obj.get("section_title")
                    or section_obj.get("heading")
                    or sid
                )
                _add_candidate(sid, title)

        generation_config = normalize_json_object(
            template_structure.get("generation_config")
        )
        ai_fields = generation_config.get("ai_fields")
        if isinstance(ai_fields, list):
            for field in ai_fields:
                field_obj = normalize_json_object(field)
                sid = (
                    field_obj.get("section_id")
                    or field_obj.get("field")
                    or field_obj.get("id")
                    or field_obj.get("field_name")
                )
                title = (
                    field_obj.get("section_title")
                    or field_obj.get("title")
                    or field_obj.get("heading")
                    or field_obj.get("name")
                    or sid
                )
                _add_candidate(sid, title)

        matched: list[str] = []
        for sid, alias in candidates:
            if alias and alias in query and sid not in matched:
                matched.append(sid)
        return matched[:8]

    async def _build_incremental_task_payload(
        self,
        *,
        task_public_id: str,
        conv,
        user_internal_id: int,
        content: str,
        intent_result: IntentResult,
    ) -> tuple[dict | None, str | None]:
        try:
            from app.agent_runtime.feature_flags import get_feature_flags

            flags = get_feature_flags()
            if not getattr(flags, "incremental_agent_enabled", False):
                return None, "feature_disabled"
        except Exception:
            return None, "feature_disabled"

        artifact = await ArtifactRepository(
            self._session
        ).get_latest_available_by_conversation(
            user_id=user_internal_id,
            conversation_id=int(conv.id),
            artifact_type="test_plan_word",
        )
        if artifact is None:
            return None, "source_artifact_missing"

        source_task = None
        try:
            result = await self._session.execute(
                select(AgentTask).where(AgentTask.id == int(artifact.task_id)).limit(1)
            )
            source_task = result.scalar_one_or_none()
        except Exception as exc:
            logger.warning(
                "MessageService: incremental source task lookup failed | "
                "task=%s | artifact=%s | err=%s",
                task_public_id,
                artifact.public_id,
                exc,
            )

        artifact_meta = normalize_json_object(getattr(artifact, "metadata_json", None))
        source_task_context = normalize_json_object(
            getattr(source_task, "task_context_json", None)
        )
        test_plan_content = normalize_json_object(
            artifact_meta.get("test_plan_content")
            or source_task_context.get("test_plan_content")
            or getattr(source_task, "plan_json", None)
        )
        if not test_plan_content:
            return None, "source_state_missing"

        template_structure = normalize_json_object(
            artifact_meta.get("template_structure")
            or source_task_context.get("template_structure")
        )
        review_result = normalize_json_object(
            artifact_meta.get("review_result")
            or getattr(source_task, "review_result_json", None)
        )
        format_check_result = normalize_json_object(
            artifact_meta.get("format_check_result")
            or source_task_context.get("format_check_result")
        )
        section_package = normalize_json_object(
            test_plan_content.get("section_package") or test_plan_content
        )
        target_section_ids = self._infer_incremental_target_section_ids(
            content,
            test_plan_content=test_plan_content,
            template_structure=template_structure,
        )
        scope_kind = self._infer_incremental_scope_kind(content)
        idempotency_raw = (
            f"{task_public_id}|{artifact.public_id}|{getattr(artifact, 'version_no', 1)}|{content}"
        )
        modification_idempotency_key = hashlib.sha256(
            idempotency_raw.encode("utf-8")
        ).hexdigest()

        incremental_intent = {
            "detected_by": "intent_router.result_modification",
            "original_intent": "result_modification",
            "confidence": float(getattr(intent_result, "confidence", 0.0) or 0.0),
            "existing_artifact": {
                "artifact_public_id": artifact.public_id,
                "task_public_id": getattr(source_task, "public_id", None),
                "version_no": int(getattr(artifact, "version_no", None) or 1),
                "source_artifact_id": getattr(artifact, "source_artifact_id", None),
                "superseded_artifact_ids": list(
                    source_task_context.get("superseded_artifact_ids") or []
                ),
                "section_package": section_package,
                "review_result": review_result,
                "last_format_check_result": format_check_result,
            },
            "scope": {
                "kind": scope_kind,
                "target_section_ids": target_section_ids,
                "request_text": content,
                "locked_section_ids": [],
                "allow_extra_sections": not bool(target_section_ids),
                "new_artifact_idempotency_key": modification_idempotency_key,
            },
            "raw_user_message": content,
        }
        logger.info(
            "MessageService: incremental task payload built | task=%s | "
            "source_artifact=%s | scope_kind=%s | target_sections=%s",
            task_public_id,
            artifact.public_id,
            scope_kind,
            target_section_ids,
        )
        return {
            "source_task": source_task,
            "source_artifact": artifact,
            "source_artifact_public_id": artifact.public_id,
            "modification_idempotency_key": modification_idempotency_key,
            "incremental_intent": incremental_intent,
            "test_plan_content": test_plan_content,
            "template_structure": template_structure,
            "review_standard": normalize_json_object(
                artifact_meta.get("review_standard")
                or source_task_context.get("review_standard")
            ),
            "review_result": review_result,
            "format_check_result": format_check_result,
            "requirement_analysis": normalize_json_object(
                artifact_meta.get("requirement_analysis")
                or source_task_context.get("requirement_analysis")
            ),
            "section_confirm_config": normalize_json_object(
                artifact_meta.get("section_confirm_config")
                or source_task_context.get("section_confirm_config")
            ),
        }, None

    @staticmethod
    def _is_explicit_memory_rule_statement(content: str) -> bool:
        """Whether text describes future rules to remember instead of a task now."""
        compact = "".join((content or "").lower().split())
        markers = ("记住", "请记住", "规则", "规范", "约束", "以后", "今后", "后续")
        patterns = (
            r"(?:以后|今后|后续).{0,24}(?:生成|编写|创建|制定|输出).{0,16}(?:测试方案|测试计划|测试文档)",
            r"(?:在|当).{0,24}(?:生成|编写|创建|制定|输出).{0,16}(?:测试方案|测试计划|测试文档).{0,12}时",
        )
        return bool(
            any(marker in compact for marker in markers)
            and any(re.search(pattern, compact) for pattern in patterns)
        )

    @staticmethod
    def _is_explicit_test_plan_request(content: str) -> bool:
        text = (content or "").lower()
        compact = "".join(text.split())
        # This fast path may create a task or ask for upload files, so it must
        # be reserved for an unambiguous *command*.  A user asking about how
        # to prepare a test plan (for example "在生成测试方案时，一般需要注意什么？")
        # is a normal knowledge question, not an instruction to generate a
        # document.  Let IntentRouter's LLM examine those ambiguous questions
        # together with conversation context instead of keyword-routing them.
        knowledge_question_markers = (
            "什么",
            "哪些",
            "如何",
            "怎么",
            "为什么",
            "是否",
            "能否",
            "可否",
            "注意什么",
            "注意事项",
            "一般需要",
            "介绍",
            "解释",
            "原则",
        )
        if "?" in compact or "？" in compact or any(
            marker in compact for marker in knowledge_question_markers
        ):
            return False
        # A rule/remember statement may mention test-plan generation without
        # asking to create a task in this turn.  Let normal chat + memory
        # learning handle it instead of returning the upload-files template.
        memory_or_rule_markers = (
            "记住", "请记住", "规则", "规范", "约束", "以后", "今后", "后续",
        )
        future_rule_patterns = (
            r"(?:以后|今后|后续).{0,24}(?:生成|编写|创建|制定|输出).{0,16}(?:测试方案|测试计划|测试文档)",
            r"(?:在|当).{0,24}(?:生成|编写|创建|制定|输出).{0,16}(?:测试方案|测试计划|测试文档).{0,12}时",
        )
        if (
            any(marker in compact for marker in memory_or_rule_markers)
            and any(re.search(pattern, compact) for pattern in future_rule_patterns)
        ):
            return False
        # \u589e\u91cf\u4fee\u6539\u8bf7\u6c42(\u5f15\u7528\u5df2\u6709\u4ea7\u7269 + \u8981\u6c42\u6539/\u6269/\u8865\u5145)\u4e0d\u7b97 "explicit \u65b0\u5efa",
        # \u5426\u5219 deterministic \u8d70 ask_for_files \u4f1a\u8ba9\u7528\u6237\u91cd\u590d\u4e0a\u4f20\u6587\u6863\u3002
        # \u8fd9\u79cd case \u5fc5\u987b\u653e\u884c\u7ed9 IntentRouter \u2192 result_modification\u3002
        reference_keywords = (
            "\u521a\u624d",       # just now
            "\u4e4b\u524d",       # before / earlier
            "\u524d\u9762",       # above / front
            "\u4e0a\u6b21",       # last time
            "\u4e0a\u9762",       # above
            "\u5df2\u751f\u6210",  # already generated
            "\u5df2\u7ecf\u751f\u6210",  # already generated (full)
            "\u5df2\u6709",       # already exist
            "\u521a\u521a",       # just
        )
        modify_keywords = (
            "\u6269\u5145",  # expand
            "\u6269\u5c55",  # extend
            "\u8d85\u51fa",  # exceed (e.g. \u8d85\u51fa100\u5b57 \u2192 already has 100, want > 100)
            "\u8ffd\u52a0",  # add
            "\u8865\u5145",  # supplement
            "\u7ec6\u5316",  # detail
            "\u5b8c\u5584",  # refine
            "\u4fee\u6539",  # modify
            "\u6539",        # change
            "\u91cd\u5199",  # rewrite
            "\u4e30\u5bcc",  # enrich
            "\u52a0",        # add (verb)
            "\u5220",        # delete (verb)
            "\u8c03\u6574",  # adjust
            "\u6539\u52a8",  # change
        )
        if (
            any(k in compact for k in reference_keywords)
            and any(k in compact for k in modify_keywords)
        ):
            return False
        action_keywords = (
            "\u751f\u6210",  # generate
            "\u7f16\u5199",  # write
            "\u521b\u5efa",  # create
            "\u5236\u5b9a",  # formulate
            "\u8f93\u51fa",  # output
            "generate",
            "create",
            "write",
        )
        plan_keywords = (
            "\u6d4b\u8bd5\u65b9\u6848",
            "\u6d4b\u8bd5\u8ba1\u5212",
            "\u6d4b\u8bd5\u6587\u6863",
            "testplan",
            "test-plan",
            "test plan",
        )
        return any(keyword in compact for keyword in action_keywords) and any(
            keyword in compact for keyword in plan_keywords
        )

    # ── Dispatch helpers ─────────────────────────────────────────

    async def _create_agent_task(
        self,
        conv,
        user_internal_id: int,
        content: str,
        now,
        user_msg_dict: dict,
        intent_result: IntentResult,
        files: list[UploadedFile] | None = None,
        attached_id_set: set[str] | None = None,
        attached_ids: list[str] | None = None,
        user_msg_internal_id: int | None = None,
        knowledge_mode_snapshot: KnowledgeMode | str = KnowledgeMode.AUTO,
    ) -> dict:
        # A few internal callers/tests construct the service via ``__new__`` and
        # inject only repositories.  Treat an absent readiness snapshot as
        # "unknown"; only an explicit failed startup probe may block creation.
        if getattr(self, "_agent_runtime_ready", None) is False:
            from app.core.exceptions import LangGraphNotReadyError

            raise LangGraphNotReadyError(
                reason=self._agent_runtime_readiness_reason or "runtime_unavailable",
                detail={
                    "error_code": "AGENT_RUNTIME_NOT_READY",
                    "task_created": False,
                },
            )

        # The production execution engine is an architectural invariant, not a
        # per-request rollout decision. Historical engine metadata is never rewritten.
        task_public_id = generate_public_id("task")
        extra_payload = dict(getattr(intent_result, "extra_payload", {}) or {})
        routing_payload = dict(extra_payload.get("capability_routing") or {})
        understanding_payload = dict(extra_payload.get("request_understanding") or {})
        execution_mode = str(routing_payload.get("execution_mode") or "")
        route_mode = str(routing_payload.get("route") or "")
        is_dynamic_agent = (
            execution_mode == "dynamic_agent"
            or route_mode == "dynamic_agent"
        )
        is_incremental_task = intent_result.intent == IntentType.RESULT_MODIFICATION
        incremental_payload: dict | None = None
        if is_incremental_task:
            incremental_payload, unavailable_reason = await self._build_incremental_task_payload(
                task_public_id=task_public_id,
                conv=conv,
                user_internal_id=user_internal_id,
                content=content,
                intent_result=intent_result,
            )
            if incremental_payload is None:
                return self._incremental_unavailable_reply(
                    user_msg_dict,
                    intent_result,
                    unavailable_reason or "source_state_missing",
                )

        if is_incremental_task:
            task_type = "incremental_test_plan"
        elif is_dynamic_agent:
            task_type = "dynamic_agent"
        else:
            task_type = "test_plan_generation"
        target_capability = str(
            routing_payload.get("target_capability")
            or understanding_payload.get("target_capability")
            or intent_result.intent.value
        )
        engine_type = "langgraph"

        # Phase 2.8R-D:graph_name / graph_version 落库且不可变。
        # 新 LangGraph 任务必须带 graph_version=settings 默认值(默认 v3);
        # 未知 / 未注册版本 → 拒绝创建任务。
        graph_name: str | None = None
        graph_version: str | None = None
        if engine_type == "langgraph":
            if is_incremental_task:
                from app.agent_runtime.incremental.subgraph import (
                    GRAPH_NAME_INCREMENTAL,
                    GRAPH_VERSION_INCREMENTAL_V3,
                )

                graph_name = GRAPH_NAME_INCREMENTAL
                graph_version = GRAPH_VERSION_INCREMENTAL_V3
            elif is_dynamic_agent:
                from app.agent_runtime.graphs.dynamic_agent import (
                    GRAPH_NAME_DYNAMIC_AGENT,
                    GRAPH_VERSION_DYNAMIC_AGENT_V3,
                )

                graph_name = GRAPH_NAME_DYNAMIC_AGENT
                graph_version = GRAPH_VERSION_DYNAMIC_AGENT_V3
            else:
                from app.agent_runtime.graphs.test_plan.constants import (
                    GRAPH_NAME_TEST_PLAN,
                )
                graph_name = GRAPH_NAME_TEST_PLAN
                graph_version = "v3"
            # 校验版本在白名单内(compile_test_plan_graph 自己会抛
            # GraphVersionNotAvailableError,这里早抛以避免创建半成品任务)。
            try:
                from app.agent_runtime.graph_registry import GraphRegistry

                GraphRegistry(name="validate_only").register(
                    graph_name, graph_version,
                    object(),
                    schema_version=1,
                )
                # 不真编译;只让 Registry 接受这个 (name, version) 键
                # 真编译由 lifespan 完成。
            except ValueError:
                # 已注册 = 已校验;不报错
                pass
            except Exception as exc:
                # 未知版本 — 拒绝任务创建(明确失败,不静默回退 legacy)
                from app.core.exceptions import GraphVersionNotAvailableError
                raise GraphVersionNotAvailableError(
                    graph_name=graph_name,
                    graph_version=graph_version,
                    detail={
                        "reason": "invalid_default_graph_version",
                        "value": graph_version,
                        "error": str(exc),
                    },
                )

        # Phase 2.8R-K 第三处修复: 把本 conversation 已 confirm-type 的
        # requirement_doc / test_plan_template 关联到新任务。
        # 之前 _create_agent_task 不接 files/attached_id_set,导致
        # task.requirement_file_id / template_file_id 永远是 None,
        # Worker 经 factory 构造的 ctx.requirement_file_id 也是 None,
        # orchestrator 调 RequirementParserTool 报 REQUIREMENT_FILE_NOT_FOUND。
        requirement_file_internal_id: int | None = None
        template_file_internal_id: int | None = None
        if is_incremental_task and incremental_payload is not None:
            source_task = incremental_payload.get("source_task")
            requirement_file_internal_id = getattr(source_task, "requirement_file_id", None)
            template_file_internal_id = getattr(source_task, "template_file_id", None)
        if files:
            attached = attached_id_set or set()
            # 优先 attached_id_set 交集;若无 attached,fallback 取本 conversation
            # 第一个匹配 file_type 的文件(避免 confirm-type 后丢链)。
            candidate_req = [
                f for f in files
                if getattr(f, "file_type", None) == "requirement_doc"
                and (not attached or getattr(f, "public_id", None) in attached)
            ]
            candidate_tpl = [
                f for f in files
                if getattr(f, "file_type", None) == "test_plan_template"
                and (not attached or getattr(f, "public_id", None) in attached)
            ]
            if not candidate_req and attached:
                # attached 过滤后没匹配 — fallback 全 conversation 内匹配
                candidate_req = [
                    f for f in files
                    if getattr(f, "file_type", None) == "requirement_doc"
                ]
                logger.warning(
                    "FALLBACK_USED | component=message_service.file_binding | "
                    "from=attached_file_filter | to=conversation_file_scan | "
                    "reason=requirement_doc_not_in_attached_set | task=%s | attached_count=%s",
                    task_public_id,
                    len(attached),
                )
            if not candidate_tpl and attached:
                candidate_tpl = [
                    f for f in files
                    if getattr(f, "file_type", None) == "test_plan_template"
                ]
                logger.warning(
                    "FALLBACK_USED | component=message_service.file_binding | "
                    "from=attached_file_filter | to=conversation_file_scan | "
                    "reason=test_plan_template_not_in_attached_set | task=%s | attached_count=%s",
                    task_public_id,
                    len(attached),
                )
            if candidate_req:
                _req_id = getattr(candidate_req[0], "id", None)
                if _req_id is not None:
                    requirement_file_internal_id = int(_req_id)
                logger.info(
                    "MessageService: 关联需求文件 | task=%s | file_id=%s | public_id=%s",
                    task_public_id, requirement_file_internal_id,
                    getattr(candidate_req[0], "public_id", None),
                )
            if candidate_tpl:
                _tpl_id = getattr(candidate_tpl[0], "id", None)
                if _tpl_id is not None:
                    template_file_internal_id = int(_tpl_id)
                logger.info(
                    "MessageService: 关联模板文件 | task=%s | file_id=%s | public_id=%s",
                    task_public_id, template_file_internal_id,
                    getattr(candidate_tpl[0], "public_id", None),
                )

        if is_dynamic_agent or is_incremental_task:
            binding_result = None
            resolved_bindings = []
        else:
            binding_result = await self._resolve_task_attachments_for_create(
                content=content,
                files=files or [],
                attached_id_set=attached_id_set,
                attached_ids=attached_ids,
            )
            if binding_result is not None and binding_result.status != "RESOLVED":
                return await self._attachment_binding_reply(
                    conv=conv,
                    user_internal_id=user_internal_id,
                    now=now,
                    user_msg_dict=user_msg_dict,
                    user_msg_internal_id=user_msg_internal_id,
                    intent_result=intent_result,
                    binding_result=binding_result,
                    files=files or [],
                )
            resolved_bindings = list(
                getattr(binding_result, "bindings", []) if binding_result else []
            )
            if binding_result is not None:
                requirement_file_internal_id = binding_result.legacy_requirement_file_id
                template_file_internal_id = binding_result.legacy_template_file_id

        # CE-05 task-freeze must be part of the INSERT payload.  A later
        # best-effort update can be skipped or overwritten by another state
        # write, which silently turns a new task into the all-false legacy
        # compatibility profile at runtime.
        try:
            from app.context_engine.feature_flags import (
                get_context_engine_flags,
                validate_full_chain_configuration,
            )
            from app.context_engine.freeze.profiles import snapshot_task_semantic_flags
            from app.context_engine.freeze.service import build_manifest, write_once_validate

            validate_full_chain_configuration()
            task_flags = snapshot_task_semantic_flags(get_context_engine_flags())
            manifest = build_manifest(
                engine=engine_type,
                decision_reason="context_flags.freeze_at_create",
                canary_bucket=None,
                workspace_key=None,
                context_engine_version=graph_version or "v3",
                task_semantic_flags=task_flags,
            )
            write_once_validate(manifest)
        except Exception as exc:
            logger.warning(
                "CE-05 task freeze build failed | task=%s | err=%s",
                task_public_id,
                exc,
            )
            raise

        task = AgentTask(
            public_id=task_public_id,
            user_id=user_internal_id,
            conversation_id=conv.id,
            project_id=getattr(conv, "project_id", None),
            task_type=task_type,
            status="created",
            title=conv.title or (
                "增量修改测试方案"
                if is_incremental_task
                else ("动态智能体任务" if is_dynamic_agent else "测试方案生成")
            ),
            user_instruction=content,
            engine_type=engine_type,
            graph_name=graph_name,
            graph_version=graph_version,
            requirement_file_id=requirement_file_internal_id,
            template_file_id=template_file_internal_id,
            # CE-01: Task 创建时锚定 Conversation workspace，避免 CPS-05
            # Scope Integrity 校验失败（task.context_workspace_key 必须等于
            # conversation:{conv.public_id}）。从 conv.public_id 推导，不依赖
            # conv.context_workspace_key 是否已 backfill，更稳。
            context_workspace_key=f"conversation:{conv.public_id}",
            context_engine_version=graph_version or "v3",
            # Phase 2.9A.26+: anchor the task to the user message that
            # triggered it.  Front-end uses this to position the task
            # run block in the timeline.
            trigger_message_id=user_msg_internal_id,
            task_context_json={"frozen_flags_manifest": manifest},
            created_at=now,
            updated_at=now,
        )
        task = await self._task_repo.create(task)
        await self._dual_write_task_file_bindings(task, resolved_bindings=resolved_bindings)

        # ── CE-05 WP-2: Task Freeze — 创建同事务写入 frozen manifest ──
        # Manifest 写失败 → 回滚整事务 → 任务创建失败（不允许任务成功但
        # Manifest 写入失败）。
        try:
            from app.context_engine.feature_flags import get_context_engine_flags
            from app.context_engine.freeze.profiles import snapshot_task_semantic_flags
            from app.context_engine.freeze.service import (
                build_manifest,
                write_once_validate,
            )

            task_flags = snapshot_task_semantic_flags(get_context_engine_flags())
            manifest = build_manifest(
                engine=engine_type,
                decision_reason="context_flags.freeze_at_create",
                canary_bucket=None,
                workspace_key=None,
                context_engine_version=graph_version or "v3",
                task_semantic_flags=task_flags,
            )
            write_once_validate(manifest)
            # 以 reserved key 写回 task_context_json（含 manifest）
            manifest_ctx = {"frozen_flags_manifest": manifest}
            write_result = self._task_repo.write_frozen_manifest(task.id, manifest_ctx)
            rows = await write_result if inspect.isawaitable(write_result) else write_result
            if rows == 0:
                # 已存在 → 校验 digest 一致性；不一致 → 409 冲突
                from app.context_engine.freeze.service import (
                    TaskFreezeConflict,
                    extract_manifest,
                    compute_flags_digest,
                )

                existing_ctx = getattr(task, "task_context_json", None)
                existing_manifest = extract_manifest(existing_ctx)
                if existing_manifest is not None:
                    existing_digest = existing_manifest.get("flags_digest")
                    if existing_digest != compute_flags_digest(task_flags):
                        raise TaskFreezeConflict("Manifest 已存在且 flags_digest 不同")
        except Exception as exc:
            logger.warning(
                "CE-05 task freeze 写入失败 | task=%s | err=%s",
                task.public_id, exc,
            )
            raise


        # Phase 4: freeze resolved Project context into this task's payload.
        project_context = await self._resolve_project_context(
            conv=conv,
            user_internal_id=user_internal_id,
            query=content,
            task_id=task.public_id,
        )

        # F016: Save task context JSON for orchestrator
        try:
            self._ensure_context_services()
            task_ctx = await self._context_svc.build_task_trigger_context(
                user_id=user_internal_id,
                conversation_id=conv.id,
                trigger_message_id=user_msg_internal_id,
                user_goal=content,
                selected_file_ids=[],
                intent=intent_result.intent.value,
                route=intent_result.route.value,
            )
            import json as _json
            task_ctx_payload = self._task_context_with_knowledge_mode(
                task_ctx.model_dump(mode="python"),
                knowledge_mode_snapshot,
            )
            # The context-enrichment write happens after the task INSERT.  Keep
            # the immutable routing decision in that payload as a recovery
            # source, so a missing JSON manifest can never turn a new task into
            # the legacy all-false compatibility profile.
            task_ctx_payload["frozen_flags_manifest"] = manifest
            if project_context:
                task_ctx_payload.update({
                    "project_id": project_context.get("project_id"),
                    "project_context": project_context,
                })
            if is_dynamic_agent:
                task_ctx_payload.update(
                    {
                        "execution_mode": "dynamic_agent",
                        "dynamic_goal": content,
                        "target_capability": target_capability,
                        "operation": understanding_payload.get("operation"),
                        "request_understanding_snapshot": understanding_payload,
                        "capability_routing_snapshot": routing_payload,
                        "attachment_refs": list(attached_ids or []),
                    }
                )
            if is_incremental_task and incremental_payload is not None:
                task_ctx_payload.update(
                    {
                        "execution_mode": "incremental_agent",
                        "target_capability": "test_plan_incremental",
                        "incremental_intent": incremental_payload["incremental_intent"],
                        "source_artifact_public_id": incremental_payload[
                            "source_artifact_public_id"
                        ],
                        "modification_idempotency_key": incremental_payload[
                            "modification_idempotency_key"
                        ],
                        "test_plan_content": incremental_payload["test_plan_content"],
                        "template_structure": incremental_payload["template_structure"],
                        "review_standard": incremental_payload["review_standard"],
                        "review_result": incremental_payload["review_result"],
                        "format_check_result": incremental_payload[
                            "format_check_result"
                        ],
                        "requirement_analysis": incremental_payload[
                            "requirement_analysis"
                        ],
                        "section_confirm_config": incremental_payload[
                            "section_confirm_config"
                        ],
                    }
                )
            ctx_json = _json.dumps(
                task_ctx_payload, ensure_ascii=False, default=str
            )
            await self._task_repo.update_context_json(
                task.id,
                ctx_json,
                allow_missing_manifest_from_patch=True,
            )
        except Exception as exc:
            logger.debug("MessageService: task_context_json save failed: %s", exc)

        event = AgentEvent(
            public_id=generate_public_id("event"),
            user_id=user_internal_id,
            conversation_id=conv.id,
            task_id=task.id,
            event_type="task_created",
            message_type="task_created",
            title="任务已创建",
            content=(
                f"已创建增量修改测试方案任务: {task.public_id}"
                if is_incremental_task
                else (
                    f"已创建动态智能体任务: {task.public_id}"
                    if is_dynamic_agent
                    else f"已创建测试方案生成任务: {task.public_id}"
                )
            ),
            status="created",
            created_at=now,
        )
        await self._event_repo.create(event)

        # Phase 2.8R-B:同一事务内写 AgentExecutionRequest(Outbox)
        # ——任务执行从 SSE 解耦;Worker 异步领取执行。
        try:
            execution_request = await self._exec_repo.enqueue_new_task(
                task_public_id=task.public_id,
                task_internal_id=task.id,
                engine_type=engine_type,
                request_type="incremental_task" if is_incremental_task else "new_task",
                graph_name=graph_name,
                graph_version=graph_version,
                payload={
                    "user_instruction": content,
                    "request_type": "incremental_task" if is_incremental_task else "new_task",
                    "intent": intent_result.intent.value,
                    "route": intent_result.route.value,
                    "graph_name": graph_name,
                    "graph_version": graph_version,
                    "execution_mode": (
                        "incremental_agent"
                        if is_incremental_task
                        else ("dynamic_agent" if is_dynamic_agent else "fixed_workflow")
                    ),
                    "incremental_intent": (
                        incremental_payload["incremental_intent"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "source_artifact_public_id": (
                        incremental_payload["source_artifact_public_id"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "modification_idempotency_key": (
                        incremental_payload["modification_idempotency_key"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "test_plan_content": (
                        incremental_payload["test_plan_content"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "template_structure": (
                        incremental_payload["template_structure"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "review_standard": (
                        incremental_payload["review_standard"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "review_result": (
                        incremental_payload["review_result"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "format_check_result": (
                        incremental_payload["format_check_result"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "requirement_analysis": (
                        incremental_payload["requirement_analysis"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "section_confirm_config": (
                        incremental_payload["section_confirm_config"]
                        if is_incremental_task and incremental_payload is not None
                        else None
                    ),
                    "dynamic_goal": content if is_dynamic_agent else None,
                    "target_capability": target_capability,
                    "operation": understanding_payload.get("operation")
                    if is_dynamic_agent
                    else None,
                    "attachment_refs": list(attached_ids or [])
                    if is_dynamic_agent
                    else [],
                    "request_understanding_snapshot": understanding_payload
                    if is_dynamic_agent
                    else {},
                    "capability_routing_snapshot": routing_payload
                    if is_dynamic_agent
                    else {},
                    "knowledge_mode_snapshot": normalize_knowledge_mode(
                        knowledge_mode_snapshot
                    ).value,
                    "project_id": (
                        project_context.get("project_id") if project_context else None
                    ),
                    "project_context": project_context,
                },
                idempotency_key=(
                    f"incremental_task|{task.public_id}"
                    if is_incremental_task
                    else f"new_task|{task.public_id}"
                ),
            )
            if execution_request is None:
                logger.info(
                    "MessageService: Outbox entry already exists for task=%s",
                    task.public_id,
                )
        except Exception:
            logger.exception(
                "Agent task transaction aborted because outbox enqueue failed | task=%s",
                task.public_id,
            )
            raise

        events_url = f"/api/agent/tasks/{task.public_id}/events"
        return {
            # Legacy fields (BACKWARD COMPAT)
            "message": user_msg_dict,
            "agent_task": {
                "task_id": task.public_id,
                "task_type": task_type,
                "status": "created",
                "events_url": events_url,
                "trigger_message_id": user_msg_dict.get("message_id"),
            },
            "agent_reply": None,
            # F013 routing fields
            "route": MessageRoute.AGENT_TASK.value,
            "intent": intent_result.intent.value,
            "requires_sse": True,
            "task_id": task.public_id,
        }

    async def _dual_write_message_attachments(
        self,
        *,
        message: Message,
        attached_ids: list[str],
        files: list[UploadedFile],
    ) -> list[str]:
        if not attached_ids:
            return []
        missing_attached_file_ids = self._missing_attached_file_ids(files, attached_ids)
        repo = getattr(self, "_message_attachment_repo", None)
        if repo is None:
            return missing_attached_file_ids
        by_public_id = {file.public_id: file for file in files}
        ordered_files = [
            by_public_id[file_public_id]
            for file_public_id in attached_ids
            if file_public_id in by_public_id
        ]
        if ordered_files:
            await repo.create_ordered_for_message(message=message, files=ordered_files)
        return missing_attached_file_ids

    async def _dual_write_task_file_bindings(
        self,
        task: AgentTask,
        *,
        resolved_bindings: list | None = None,
    ) -> None:
        repo = getattr(self, "_task_file_binding_repo", None)
        if repo is None:
            return
        if resolved_bindings:
            persist = getattr(repo, "persist_resolved_bindings", None)
            if persist is not None:
                await persist(task=task, bindings=resolved_bindings)
                return
        legacy_projection = getattr(repo, "create_legacy_projection", None)
        if legacy_projection is not None:
            await legacy_projection(task)

    async def _resolve_task_attachments_for_create(
        self,
        *,
        content: str,
        files: list[UploadedFile],
        attached_id_set: set[str] | None,
        attached_ids: list[str] | None,
    ):
        if not files:
            return None
        if not hasattr(self, "_session"):
            return None
        by_public_id = {getattr(file, "public_id", None): file for file in files}
        ordered_files = [
            by_public_id[file_public_id]
            for file_public_id in (attached_ids or [])
            if file_public_id in by_public_id
        ]
        if not ordered_files:
            attached = attached_id_set or set()
            ordered_files = [
                file
                for file in files
                if not attached or getattr(file, "public_id", None) in attached
            ]
        try:
            from app.services.task_attachment_resolver import TaskAttachmentResolver

            return await TaskAttachmentResolver(self._session).resolve(
                task_type="test_plan_generation",
                user_message=content,
                ordered_files=ordered_files,
                conversation_files=files,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=message_service.attachment_binding | "
                "from=attachment_resolver | to=legacy_file_hints | "
                "reason=resolver_exception | attached_count=%s | "
                "ordered_file_count=%s | err_type=%s",
                len(attached_ids or []),
                len(ordered_files),
                exc.__class__.__name__,
            )
            logger.warning(
                "MessageService: task attachment resolver failed, using legacy hints | err=%s",
                exc,
            )
            return None

    async def _attachment_binding_reply(
        self,
        *,
        conv,
        user_internal_id: int,
        now,
        user_msg_dict: dict,
        user_msg_internal_id: int | None,
        intent_result: IntentResult,
        binding_result,
        files: list[UploadedFile],
    ) -> dict:
        payload = self._assistant_payload(intent_result)
        payload["route"] = MessageRoute.CHAT_REPLY.value
        payload.update({
            "attachment_binding_status": binding_result.status,
            "attachment_binding_reason": binding_result.reason,
            "attachment_binding_error_code": binding_result.error_code,
            "ambiguous_role": binding_result.ambiguous_role,
        })
        reply_text = "需要先确认附件用途后才能创建测试方案任务。"
        agent_msg = Message(
            public_id=generate_public_id("message"),
            user_id=user_internal_id,
            conversation_id=conv.id,
            role="agent",
            message_type="agent_text",
            content=reply_text,
            payload_json=payload,
            status="sent",
            reply_to_message_id=user_msg_internal_id,
            created_at=now,
            updated_at=now,
        )
        agent_msg = await self._msg_repo.create(agent_msg)
        return {
            "message": user_msg_dict,
            "agent_task": None,
            "agent_reply": self._to_detail(agent_msg, conv.public_id, files),
            "route": MessageRoute.CHAT_REPLY.value,
            "intent": intent_result.intent.value,
            "requires_sse": False,
        }

    async def _create_assistant_reply(
        self,
        conv,
        user_internal_id: int,
        content: str,
        now,
        user_msg_dict: dict,
        intent_result: IntentResult,
        files: list[UploadedFile],
        *,
        chat_context: ChatContext | None = None,
        intent_context: IntentContext | None = None,
        attached_id_set: set[str] | None = None,
        attached_ids: list[str] | None = None,
        user_msg_internal_id: int | None = None,
        knowledge_mode_snapshot: KnowledgeMode | str = KnowledgeMode.AUTO,
    ) -> dict:
        retrieval_plan = self._build_retrieval_plan(
            content,
            intent_result,
            files=files,
            attached_ids=attached_ids or [],
            knowledge_mode_snapshot=knowledge_mode_snapshot,
        )
        # F026 + request-understanding: KB direct-answer only when retrieval
        # planning allows MaaS, not for every ordinary chat question.
        kb_reply: str | None = None
        kb_meta: dict | None = None
        kb_decision = None
        if retrieval_plan.maas == "required":
            kb_reply, kb_meta, kb_decision = await self._company_rag_reply(
                user_internal_id=user_internal_id,
                content=content,
                intent_result=intent_result,
            )
        elif self._should_attempt_kb(intent_result, retrieval_plan):
            kb_reply, kb_meta, kb_decision = await self._try_kb_direct_reply(
                user_internal_id, content, intent_result,
            )

        # Pick the reply text
        chat_diagnostics: dict[str, Any] = {}
        if kb_reply is not None:
            reply = kb_reply
        elif intent_result.route == MessageRoute.EXISTING_TASK_ACTION:
            task_summary = None
            if intent_context is not None:
                task_summary = intent_context.latest_task_summary
            reply = self._build_existing_task_reply(task_summary)
        elif intent_result.intent == IntentType.KNOWLEDGE_QUESTION:
            # F026: KB miss → LLM polish with KB context (if any snippets)
            reply = await self._chat_reply(
                content,
                chat_context=self._maybe_inject_kb_context(chat_context, kb_decision),
                user_id=user_internal_id,
                conversation_public_id=conv.public_id,
                diagnostics=chat_diagnostics,
            )
        elif intent_result.route == MessageRoute.CHAT_REPLY:
            # F026: chat_reply may have KB snippets even when not direct-answer
            effective_chat_context = self._maybe_inject_kb_context(chat_context, kb_decision)
            image_reply = await self._current_image_vision_reply(
                content=content,
                user_id=user_internal_id,
                files=files,
                attached_id_set=attached_id_set,
                chat_context=effective_chat_context,
            )
            reply = image_reply or await self._chat_reply(
                content,
                chat_context=effective_chat_context,
                user_id=user_internal_id,
                conversation_public_id=conv.public_id,
                diagnostics=chat_diagnostics,
                call_site=self._chat_call_site(intent_result),
            )
        else:
            reply = (
                intent_result.reply_message
                or self._default_reply_for(intent_result.intent)
            )

        # Persist the assistant message
        payload = {
            "attached_file_ids": [],
            "route": intent_result.route.value,
            "intent": intent_result.intent.value,
            "intent_confidence": intent_result.confidence,
            "reason": intent_result.reason,
            "need_files": intent_result.need_files,
            "required_file_types": intent_result.required_file_types,
            "missing_file_types": intent_result.missing_file_types,
            "knowledge_mode_snapshot": normalize_knowledge_mode(
                knowledge_mode_snapshot
            ).value,
            "retrieval_plan": retrieval_plan.model_dump(mode="python"),
        }
        payload.update(getattr(intent_result, "extra_payload", {}) or {})
        payload.update(chat_diagnostics)
        if intent_result.intent == IntentType.DOCUMENT_QUESTION:
            citations = await self._document_citations_from_snapshot(
                user_id=user_internal_id,
                snapshot_public_id=(
                    (chat_diagnostics.get("chat_context_engine") or {})
                    .get("snapshot_public_id")
                ),
            )
            if citations:
                payload["document_citations"] = citations
        if kb_meta:
            payload["kb_direct_answer"] = kb_meta
        agent_msg = Message(
            public_id=generate_public_id("message"),
            user_id=user_internal_id,
            conversation_id=conv.id,
            role="agent",
            message_type="agent_text",
            content=reply,
            payload_json=payload,
            status="sent",
            # Phase 2.9A.26+: agent replies are anchored to the
            # immediately preceding user message.  The trigger
            # user message has already been persisted above, so its
            # internal id is stable.
            reply_to_message_id=user_msg_internal_id,
            created_at=now,
            updated_at=now,
        )
        agent_msg = await self._msg_repo.create(agent_msg)

        return {
            # Legacy fields
            "message": user_msg_dict,
            "agent_task": None,
            "agent_reply": self._to_detail(agent_msg, "", files),
            # F013 routing fields
            "route": intent_result.route.value,
            "intent": intent_result.intent.value,
            "requires_sse": False,
            "task_id": None,
        }

    async def _stream_assistant_reply(
        self,
        conv,
        user_internal_id: int,
        content: str,
        now,
        user_msg_dict: dict,
        intent_result: IntentResult,
        files: list[UploadedFile],
        *,
        chat_context: ChatContext | None = None,
        intent_context: IntentContext | None = None,
        attached_id_set: set[str] | None = None,
        attached_ids: list[str] | None = None,
        user_msg_internal_id: int | None = None,
        knowledge_mode_snapshot: KnowledgeMode | str = KnowledgeMode.AUTO,
    ) -> AsyncIterator[dict]:
        payload = self._assistant_payload(intent_result)
        retrieval_plan = self._build_retrieval_plan(
            content,
            intent_result,
            files=files,
            attached_ids=attached_ids or [],
            knowledge_mode_snapshot=knowledge_mode_snapshot,
        )
        payload["knowledge_mode_snapshot"] = normalize_knowledge_mode(
            knowledge_mode_snapshot
        ).value
        payload["retrieval_plan"] = retrieval_plan.model_dump(mode="python")
        agent_message_id = generate_public_id("message")
        preview = {
            "message_id": agent_message_id,
            "conversation_id": "",
            "role": "agent",
            "message_type": "agent_text",
            "content": "",
            "payload": payload,
            "attached_files": [],
            "timestamp": now.isoformat() if now else "",
            "route": intent_result.route.value,
            "intent": intent_result.intent.value,
            "intent_confidence": intent_result.confidence,
        }
        yield {
            "event": "agent_reply_created",
            "data": {
                "message": preview,
                "route": intent_result.route.value,
                "intent": intent_result.intent.value,
                "requires_sse": False,
                "task_id": None,
            },
        }

        chunks: list[str] = []
        if intent_result.route == MessageRoute.EXISTING_TASK_ACTION:
            reply = self._build_existing_task_reply(getattr(intent_context, 'latest_task_summary', None) if intent_context else None)
            chunks.append(reply)
            yield {
                "event": "agent_text_delta",
                "data": {"message_id": agent_message_id, "delta": reply},
            }
        elif intent_result.intent == IntentType.DOCUMENT_QUESTION:
            # Keep the document-QA call non-streaming so the completed Context
            # snapshot can be turned into deterministic source citations before
            # the terminal SSE event persists the assistant message.
            document_diagnostics: dict[str, Any] = {}
            reply = await self._chat_reply(
                content,
                chat_context=chat_context,
                user_id=user_internal_id,
                conversation_public_id=conv.public_id,
                diagnostics=document_diagnostics,
                call_site="document.qa",
            )
            payload.update(document_diagnostics)
            citations = await self._document_citations_from_snapshot(
                user_id=user_internal_id,
                snapshot_public_id=(
                    (document_diagnostics.get("chat_context_engine") or {})
                    .get("snapshot_public_id")
                ),
            )
            if citations:
                payload["document_citations"] = citations
            chunks.append(reply)
            yield {
                "event": "agent_text_delta",
                "data": {"message_id": agent_message_id, "delta": reply},
            }
        elif (
            intent_result.route == MessageRoute.CHAT_REPLY
            and retrieval_plan.maas != "required"
            and self._has_current_image_attachments(
                files,
                attached_id_set,
            )
        ):
            image_reply = await self._current_image_vision_reply(
                content=content,
                user_id=user_internal_id,
                files=files,
                attached_id_set=attached_id_set,
                chat_context=chat_context,
            )
            reply = image_reply or await self._chat_reply(
                content,
                chat_context=chat_context,
                user_id=user_internal_id,
                conversation_public_id=conv.public_id,
            )
            chunks.append(reply)
            yield {
                "event": "agent_text_delta",
                "data": {"message_id": agent_message_id, "delta": reply},
            }
        elif intent_result.route == MessageRoute.CHAT_REPLY and retrieval_plan.maas == "required":
            kb_reply, kb_meta, kb_decision = await self._company_rag_reply(
                user_internal_id=user_internal_id,
                content=content,
                intent_result=intent_result,
            )
            if kb_meta is not None:
                payload["kb_direct_answer"] = kb_meta
            chunks.append(kb_reply)
            yield {
                "event": "agent_text_delta",
                "data": {"message_id": agent_message_id, "delta": kb_reply},
            }
        elif intent_result.route == MessageRoute.CHAT_REPLY:
            # F026: same path; possibly with KB snippets injected
            if retrieval_plan.maas == "required":
                kb_reply, kb_meta, kb_decision = await self._company_rag_reply(
                    user_internal_id=user_internal_id,
                    content=content,
                    intent_result=intent_result,
                )
            elif self._should_attempt_kb(intent_result, retrieval_plan):
                kb_reply, kb_meta, kb_decision = await self._try_kb_direct_reply(
                    user_internal_id, content, intent_result,
                )
            else:
                kb_reply, kb_meta, kb_decision = None, None, None
            if kb_meta is not None:
                payload["kb_direct_answer"] = kb_meta
            if kb_reply is not None:
                chunks.append(kb_reply)
                yield {
                    "event": "agent_text_delta",
                    "data": {"message_id": agent_message_id, "delta": kb_reply},
                }
            else:
                effective_chat_context = self._maybe_inject_kb_context(
                    chat_context, kb_decision,
                )
                reasoning_filter = ReasoningBlockFilter()
                # Chat SSE Lifecycle Fix — Phase 1 runtime trace (no control flow change).
                logger.info(
                    "CHAT_SSE_LLM_STREAM_ENTER | conv=%d | agent_message_id=%s",
                    conv.id, agent_message_id,
                )
                _stream_chunk_count = 0
                _stream_text_len = 0
                async for chunk in self._chat_reply_stream(
                    content,
                    chat_context=effective_chat_context,
                    user_id=user_internal_id,
                    conversation_public_id=conv.public_id,
                    call_site=self._chat_call_site(intent_result),
                ):
                    if not chunk:
                        continue
                    visible_chunk = reasoning_filter.feed(chunk)
                    if not visible_chunk:
                        continue
                    _stream_chunk_count += 1
                    _stream_text_len += len(visible_chunk)
                    chunks.append(visible_chunk)
                    yield {
                        "event": "agent_text_delta",
                        "data": {"message_id": agent_message_id, "delta": visible_chunk},
                    }
                tail_chunk = reasoning_filter.finish()
                if tail_chunk:
                    chunks.append(tail_chunk)
                    yield {
                        "event": "agent_text_delta",
                        "data": {"message_id": agent_message_id, "delta": tail_chunk},
                    }
                logger.info(
                    "CHAT_SSE_LLM_STREAM_EXIT | conv=%d | agent_message_id=%s | chunks=%d | text_len=%d",
                    conv.id, agent_message_id, _stream_chunk_count, _stream_text_len,
                )
        elif intent_result.intent == IntentType.KNOWLEDGE_QUESTION:
            # F026: KB direct-answer or LLM polish for KB questions
            if retrieval_plan.maas == "required":
                kb_reply, kb_meta, kb_decision = await self._company_rag_reply(
                    user_internal_id=user_internal_id,
                    content=content,
                    intent_result=intent_result,
                )
            elif self._should_attempt_kb(intent_result, retrieval_plan):
                kb_reply, kb_meta, kb_decision = await self._try_kb_direct_reply(
                    user_internal_id, content, intent_result,
                )
            else:
                kb_reply, kb_meta, kb_decision = None, None, None
            if kb_meta is not None:
                payload["kb_direct_answer"] = kb_meta
            if kb_reply is not None:
                chunks.append(kb_reply)
                yield {
                    "event": "agent_text_delta",
                    "data": {"message_id": agent_message_id, "delta": kb_reply},
                }
            else:
                # KB miss → fall back to LLM polish (with KB snippets if any)
                effective_chat_context = self._maybe_inject_kb_context(
                    chat_context, kb_decision,
                )
                reasoning_filter = ReasoningBlockFilter()
                async for chunk in self._chat_reply_stream(
                    content,
                    chat_context=effective_chat_context,
                    user_id=user_internal_id,
                    conversation_public_id=conv.public_id,
                ):
                    if not chunk:
                        continue
                    visible_chunk = reasoning_filter.feed(chunk)
                    if not visible_chunk:
                        continue
                    chunks.append(visible_chunk)
                    yield {
                        "event": "agent_text_delta",
                        "data": {"message_id": agent_message_id, "delta": visible_chunk},
                    }
                tail_chunk = reasoning_filter.finish()
                if tail_chunk:
                    chunks.append(tail_chunk)
                    yield {
                        "event": "agent_text_delta",
                        "data": {"message_id": agent_message_id, "delta": tail_chunk},
                    }
        else:
            reply = (
                intent_result.reply_message
                or self._default_reply_for(intent_result.intent)
            )
            chunks.append(reply)
            yield {
                "event": "agent_text_delta",
                "data": {"message_id": agent_message_id, "delta": reply},
            }

        reply = "".join(chunks).strip()
        if not reply:
            logger.warning(
                "FALLBACK_USED | component=message_service.stream_finalize | "
                "from=stream_chunks | to=static_chat_fallback | "
                "reason=no_final_text | user_id=%s | conversation=%s",
                user_internal_id,
                conv_public_id,
            )
            reply = CHAT_FALLBACK_REPLY
        agent_msg = Message(
            public_id=agent_message_id,
            user_id=user_internal_id,
            conversation_id=conv.id,
            role="agent",
            message_type="agent_text",
            content=reply,
            payload_json=payload,
            status="sent",
            # Phase 2.9A.26+: stream path also anchors agent reply to
            # the user message that triggered it.
            reply_to_message_id=user_msg_internal_id,
            created_at=now,
            updated_at=now,
        )
        # Chat SSE Lifecycle Fix — Phase 1 runtime trace (no control flow change).
        logger.info(
            "CHAT_SSE_AGENT_PERSIST_START | conv=%d | agent_message_id=%s | content_len=%d",
            conv.id, agent_message_id, len(reply),
        )
        agent_msg = await self._msg_repo.create(agent_msg)
        logger.info(
            "CHAT_SSE_AGENT_PERSIST_END | conv=%d | agent_message_id=%s | id=%d",
            conv.id, agent_message_id, getattr(agent_msg, "id", -1),
        )
        agent_detail = self._to_detail(agent_msg, "", files)
        logger.info(
            "CHAT_SSE_TERMINAL_EMIT | conv=%d | event=agent_text_done | agent_message_id=%s",
            conv.id, agent_message_id,
        )
        yield {
            "event": "agent_text_done",
            "data": {
                "message": user_msg_dict,
                "agent_task": None,
                "agent_reply": agent_detail,
                "route": intent_result.route.value,
                "intent": intent_result.intent.value,
                "requires_sse": False,
                "task_id": None,
            },
        }

    async def _chat_reply(
        self,
        user_content: str,
        *,
        chat_context: ChatContext | None = None,
        user_id: int | None = None,
        conversation_public_id: str | None = None,
        diagnostics: dict[str, Any] | None = None,
        call_site: str = "chat.reply",
    ) -> str:
        if self._chat is None:
            logger.warning(
                "FALLBACK_USED | component=message_service.chat_reply | "
                "from=chat_llm_service | to=static_chat_fallback | "
                "reason=chat_service_not_configured | user_id=%s",
                user_id,
            )
            return CHAT_FALLBACK_REPLY
        try:
            kwargs: dict[str, Any] = {
                "chat_context": chat_context,
                "user_id": user_id,
                "conversation_public_id": conversation_public_id,
            }
            try:
                if "diagnostics" in inspect.signature(self._chat.generate_reply).parameters:
                    kwargs["diagnostics"] = diagnostics
                if "call_site" in inspect.signature(self._chat.generate_reply).parameters:
                    kwargs["call_site"] = call_site
            except (TypeError, ValueError):
                kwargs["diagnostics"] = diagnostics
                kwargs["call_site"] = call_site
            reply = await self._chat.generate_reply(user_content, **kwargs)
            stripped = ReasoningBlockFilter.strip_text(reply)
            if stripped:
                return stripped
            logger.warning(
                "FALLBACK_USED | component=message_service.chat_reply | "
                "from=chat_llm_service | to=static_chat_fallback | "
                "reason=empty_reply | user_id=%s",
                user_id,
            )
            return CHAT_FALLBACK_REPLY

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=message_service.chat_reply | "
                "from=chat_llm_service | to=static_chat_fallback | "
                "reason=chat_service_exception | user_id=%s | err_type=%s | err=%s",
                user_id,
                type(exc).__name__,
                str(exc)[:300],
            )
            return CHAT_FALLBACK_REPLY

    @staticmethod
    def _chat_call_site(intent_result: IntentResult) -> str:
        return (
            "document.qa"
            if intent_result.intent == IntentType.DOCUMENT_QUESTION
            else "chat.reply"
        )

    async def _document_citations_from_snapshot(
        self,
        *,
        user_id: int,
        snapshot_public_id: str | None,
    ) -> list[dict[str, Any]]:
        """Return display-safe citations for chunks selected in one snapshot."""
        if not snapshot_public_id:
            return []
        try:
            from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
            from app.models.context_snapshot import ContextSnapshot

            snapshot = (await self._session.execute(
                select(ContextSnapshot).where(
                    ContextSnapshot.public_id == str(snapshot_public_id),
                    ContextSnapshot.user_id == user_id,
                )
            )).scalar_one_or_none()
            refs = list(getattr(snapshot, "included_refs_json", None) or []) if snapshot else []
            chunk_ids = [
                str(ref.get("source_ref"))
                for ref in refs
                if isinstance(ref, dict)
                and str(ref.get("item_id") or "").startswith("conversation_document:")
                and ref.get("source_ref")
            ]
            full_document_ids = [
                str(ref.get("source_ref"))
                for ref in refs
                if isinstance(ref, dict)
                and str(ref.get("item_id") or "").startswith("conversation_document_full:")
                and ref.get("source_ref")
            ]
            if not chunk_ids and not full_document_ids:
                return []
            if full_document_ids:
                documents = (await self._session.execute(
                    select(ContextIndexDocument).where(
                        ContextIndexDocument.public_id.in_(full_document_ids),
                        ContextIndexDocument.user_id == user_id,
                        ContextIndexDocument.deleted_at.is_(None),
                    )
                )).scalars().all()
                by_document = {document.public_id: document for document in documents}
                return [
                    {
                        "source_id": document.source_public_id,
                        "document_id": document.public_id,
                        "chunk_id": None,
                        "title": document.title or "Untitled material",
                        "version": document.source_version,
                        "section": None,
                        "role": document.source_type,
                        "whole_document": True,
                    }
                    for document_id in full_document_ids
                    if (document := by_document.get(document_id)) is not None
                ]
            rows = (await self._session.execute(
                select(ContextIndexDocument, ContextIndexChunk)
                .join(ContextIndexChunk, ContextIndexChunk.document_id == ContextIndexDocument.id)
                .where(
                    ContextIndexChunk.public_id.in_(chunk_ids),
                    ContextIndexChunk.user_id == user_id,
                    ContextIndexChunk.deleted_at.is_(None),
                    ContextIndexDocument.deleted_at.is_(None),
                )
            )).all()
            by_chunk = {chunk.public_id: (document, chunk) for document, chunk in rows}
            citations: list[dict[str, Any]] = []
            seen: set[tuple[str, str, str]] = set()
            for chunk_id in chunk_ids:
                pair = by_chunk.get(chunk_id)
                if pair is None:
                    continue
                document, chunk = pair
                key = (document.public_id, chunk.public_id, document.source_version)
                if key in seen:
                    continue
                seen.add(key)
                citations.append(
                    {
                        "source_id": document.source_public_id,
                        "document_id": document.public_id,
                        "chunk_id": chunk.public_id,
                        "title": document.title or "未命名资料",
                        "version": document.source_version,
                        "section": chunk.section_path or None,
                        "role": document.source_type,
                    }
                )
            return citations
        except Exception as exc:  # noqa: BLE001 - attribution cannot break reply
            logger.warning(
                "DOCUMENT_QA_CITATION_BUILD_FAILED | snapshot=%s | err=%s",
                snapshot_public_id,
                type(exc).__name__,
            )
            return []

    async def _current_image_vision_reply(
        self,
        *,
        content: str,
        user_id: int,
        files: list[UploadedFile],
        attached_id_set: set[str] | None,
        chat_context: ChatContext | None = None,
    ) -> str | None:
        try:
            service = self._current_image_vision_service
            if service is None:
                from app.services.current_message_image_vision_service import CurrentMessageImageVisionService
                from app.services.settings_service import SettingsService

                provider = await SettingsService(self._session).build_llm_config_provider(user_id)
                service = CurrentMessageImageVisionService(
                    self._session,
                    main_llm_client=self._llm,
                    main_model_supports_vision=bool(getattr(provider, "supports_vision", False)),
                    context_llm_invoker=self._context_llm_invoker,
                    session_factory=self._session_factory,
                    chat_service=self._chat,
                )
            result = await service.generate_reply(
                user_content=content,
                user_id=user_id,
                files=files,
                attached_id_set=attached_id_set,
                chat_context=chat_context,
            )
            if getattr(result, "handled", False):
                reply = str(getattr(result, "reply", "") or "").strip()
                if reply:
                    return reply
                logger.warning(
                    "FALLBACK_USED | component=message_service.image_vision | "
                    "from=current_message_image_vision | to=static_chat_fallback | "
                    "reason=empty_vision_reply | user_id=%s",
                    user_id,
                )
                return CHAT_FALLBACK_REPLY
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=message_service.image_vision | "
                "from=current_message_image_vision | to=chat_reply_path | "
                "reason=vision_exception | user_id=%s | err_type=%s | err=%s",
                user_id,
                type(exc).__name__,
                str(exc)[:200],
            )
            return None

    @staticmethod
    def _has_current_image_attachments(
        files: list[UploadedFile],
        attached_id_set: set[str] | None,
    ) -> bool:
        if not files:
            return False
        from app.services.file_capability_registry import FileProcessingCapabilityRegistry

        registry = FileProcessingCapabilityRegistry()
        for file in files:
            if attached_id_set and getattr(file, "public_id", None) not in attached_id_set:
                continue
            if registry.for_extension(getattr(file, "file_ext", "")).can_vision:
                return True
        return False

    async def _chat_reply_stream(
        self,
        user_content: str,
        *,
        chat_context: ChatContext | None = None,
        user_id: int | None = None,
        conversation_public_id: str | None = None,
        call_site: str = "chat.reply",
    ) -> AsyncIterator[str]:
        if self._chat is None:
            logger.warning(
                "FALLBACK_USED | component=message_service.chat_stream | "
                "from=chat_llm_stream | to=static_chat_fallback | "
                "reason=chat_service_not_configured | user_id=%s",
                user_id,
            )
            yield CHAT_FALLBACK_REPLY
            return
        stream_reply = getattr(self._chat, "stream_reply", None)
        if stream_reply is None:
            logger.warning(
                "FALLBACK_USED | component=message_service.chat_stream | "
                "from=chat_llm_stream | to=non_stream_chat_reply | "
                "reason=stream_reply_not_configured | user_id=%s",
                user_id,
            )
            yield await self._chat_reply(
                user_content, chat_context=chat_context, user_id=user_id,
                conversation_public_id=conversation_public_id,
                call_site=call_site,
            )
            return
        try:
            emitted = False
            kwargs = {"chat_context": chat_context}
            try:
                sig = inspect.signature(stream_reply)
                if "user_id" in sig.parameters:
                    kwargs["user_id"] = user_id
                if "conversation_public_id" in sig.parameters:
                    kwargs["conversation_public_id"] = conversation_public_id
                elif user_id is not None:
                    logger.warning(
                        "FALLBACK_USED | component=message_service.chat_stream | "
                        "from=user_aware_stream_args | to=legacy_stream_args | "
                        "reason=stream_reply_does_not_accept_user_id | user_id=%s",
                        user_id,
                    )
                if "call_site" in sig.parameters:
                    kwargs["call_site"] = call_site
            except (TypeError, ValueError):
                kwargs["user_id"] = user_id
            async for chunk in stream_reply(user_content, **kwargs):
                if not chunk:
                    continue
                emitted = True
                yield str(chunk)
            if not emitted:
                logger.warning(
                    "FALLBACK_USED | component=message_service.chat_stream | "
                    "from=chat_llm_stream | to=static_chat_fallback | "
                    "reason=empty_stream | user_id=%s",
                    user_id,
                )
                yield CHAT_FALLBACK_REPLY
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=message_service.chat_stream | "
                "from=chat_llm_stream | to=static_chat_fallback | "
                "reason=stream_exception | user_id=%s | err_type=%s | err=%s",
                user_id,
                type(exc).__name__,
                str(exc)[:300],
            )
            yield CHAT_FALLBACK_REPLY

    async def _try_kb_direct_reply(
        self,
        user_internal_id: int,
        content: str,
        intent_result: IntentResult,
        *,
        bypass_trigger_gate: bool = False,
    ) -> tuple[str | None, dict | None, Any]:
        """Attempt KB direct answer for chat_reply or knowledge_question.

        Returns ``(reply, meta, decision)``. When KB cannot short-circuit the
        LLM (unavailable / low confidence / wrong route), returns
        ``(None, None, None)`` so the caller proceeds with normal chat.

        ``decision`` is the :class:`KnowledgeDecision` returned by
        :meth:`KnowledgeAnswerService.decide` so the caller can reuse its
        ``context['knowledge_snippets']`` for the LLM-polish fallback path.
        """
        if not bypass_trigger_gate and not self._kb_trigger_eligible(intent_result):
            return None, None, None
        try:
            svc = KnowledgeAnswerService(self._session)
            decision = await svc.decide(
                user_internal_id=user_internal_id,
                query=content,
                route=MessageRoute.CHAT_REPLY
                if bypass_trigger_gate
                else intent_result.route,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=message_service.kb_direct_reply | "
                "from=knowledge_answer_service | to=llm_chat_path | "
                "reason=kb_decision_exception | user=%d | err_type=%s",
                user_internal_id,
                exc.__class__.__name__,
            )
            logger.warning(
                "MessageService._try_kb_direct_reply: 知识库判定异常 | user=%d | err=%s",
                user_internal_id,
                exc.__class__.__name__,
            )
            return None, None, None

        if decision.should_call_llm or not decision.direct_answer:
            return None, {
                "attempted": True,
                "hit": False,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "error_code": decision.error_code,
            }, decision
        meta = {
            "attempted": True,
            "hit": True,
            "confidence": decision.confidence,
            "source_attribution": decision.source_attribution,
        }
        return decision.direct_answer, meta, decision

    async def _company_rag_reply(
        self,
        *,
        user_internal_id: int,
        content: str,
        intent_result: IntentResult,
    ) -> tuple[str, dict, Any]:
        reply, meta, decision = await self._try_kb_direct_reply(
            user_internal_id,
            content,
            intent_result,
            bypass_trigger_gate=True,
        )
        if reply:
            strict_meta = dict(meta or {})
            strict_meta["strict"] = True
            return reply, strict_meta, decision

        error_code = (
            (meta or {}).get("error_code")
            or getattr(decision, "error_code", None)
            or ""
        )
        unavailable = bool(error_code) or "unavailable" in str(
            (meta or {}).get("reason", "")
        ).lower()
        strict_meta = dict(meta or {})
        strict_meta.update(
            {
                "attempted": True,
                "hit": False,
                "strict": True,
                "error_code": error_code or None,
            }
        )
        if unavailable:
            strict_meta["reason"] = strict_meta.get("reason") or "maas_unavailable"
            return STRICT_MAAS_UNAVAILABLE_TEXT, strict_meta, decision
        strict_meta["reason"] = strict_meta.get("reason") or "maas_no_hit"
        return STRICT_MAAS_NO_HIT_TEXT, strict_meta, decision

    def _build_retrieval_plan(
        self,
        content: str,
        intent_result: IntentResult,
        *,
        files: list[UploadedFile],
        attached_ids: list[str],
        knowledge_mode_snapshot: KnowledgeMode | str,
    ) -> RetrievalPlan:
        understanding = build_request_understanding(
            content,
            intent_result=intent_result,
            files=files,
            attached_file_ids=attached_ids,
        )
        return RetrievalPlanner().plan(
            content,
            understanding,
            knowledge_mode=knowledge_mode_snapshot,
            attached_file_ids=attached_ids,
        )

    @staticmethod
    def _should_attempt_kb(
        intent_result: IntentResult,
        retrieval_plan: RetrievalPlan,
    ) -> bool:
        # Company RAG is supporting evidence for test-plan workflows only.
        # Ordinary chat uses project/context-engine retrieval and never returns
        # a provider response as a direct answer.
        del intent_result, retrieval_plan
        return False

    @staticmethod
    def _kb_trigger_eligible(intent_result: IntentResult) -> bool:
        """Decide whether KB direct-answer should be attempted for this intent.

        F026: gate widened from ``route == CHAT_REPLY`` only to also accept
        ``intent == KNOWLEDGE_QUESTION``. Everything else falls through.
        """
        return (
            intent_result.route == MessageRoute.CHAT_REPLY
            or intent_result.intent == IntentType.KNOWLEDGE_QUESTION
        )

    @staticmethod
    def _maybe_inject_kb_context(
        chat_context: ChatContext | None,
        kb_decision: Any,
    ) -> ChatContext | None:
        """Return a copy of ``chat_context`` with KB snippets attached.

        ``kb_decision.context["knowledge_snippets"]`` (when present) is
        copied onto the returned ChatContext so the LLM polish path can
        cite them. Returns ``chat_context`` untouched when there are no
        snippets to inject, or ``None`` when no chat context was provided.
        """
        if chat_context is None or kb_decision is None:
            return chat_context
        ctx = getattr(kb_decision, "context", None)
        if not isinstance(ctx, dict):
            return chat_context
        snippets = ctx.get("knowledge_snippets")
        if not snippets:
            return chat_context
        return chat_context.model_copy(
            update={
                "knowledge_snippets": list(snippets),
                "knowledge_source_count": len(snippets),
            }
        )

    @staticmethod
    def _assistant_payload(intent_result: IntentResult) -> dict:
        payload = {
            "attached_file_ids": [],
            "route": intent_result.route.value,
            "intent": intent_result.intent.value,
            "intent_confidence": intent_result.confidence,
            "reason": intent_result.reason,
            "need_files": intent_result.need_files,
            "required_file_types": intent_result.required_file_types,
            "missing_file_types": intent_result.missing_file_types,
        }
        payload.update(getattr(intent_result, "extra_payload", {}) or {})
        return payload

    @staticmethod
    def _default_reply_for(intent: IntentType) -> str:
        return INTENT_DEFAULT_REPLIES.get(
            intent, INTENT_DEFAULT_REPLIES[IntentType.UNKNOWN]
        )

    # ── Detail shaping (unchanged) ───────────────────────────────

    @staticmethod
    def _file_to_summary(file: UploadedFile) -> dict:
        return {
            "file_id": file.public_id,
            "original_name": file.original_name,
            "file_ext": file.file_ext,
            "file_size": file.file_size,
            "file_type": file.file_type,
            "upload_status": file.upload_status,
        }

    @staticmethod
    def _attached_file_ids(msg: Message) -> list[str]:
        payload = normalize_json_object(msg.payload_json)
        ids = payload.get("attached_file_ids", [])
        if not isinstance(ids, list):
            return []
        return [str(file_id) for file_id in ids]

    @classmethod
    def _attached_files(
        cls, msg: Message, files: list[UploadedFile] | None
    ) -> list[dict]:
        attached_ids = cls._attached_file_ids(msg)
        if not attached_ids or not files:
            return []
        by_public_id = {file.public_id: file for file in files}
        return [
            cls._file_to_summary(by_public_id[file_public_id])
            for file_public_id in attached_ids
            if file_public_id in by_public_id
        ]

    @classmethod
    def _to_detail(
        cls,
        msg: Message,
        conv_public_id: str = "",
        files: list[UploadedFile] | None = None,
    ) -> dict:
        # Surface F013 routing metadata when present in payload_json.
        payload = normalize_json_object(msg.payload_json)
        route = str(payload.get("route", "") or "")
        intent = str(payload.get("intent", "") or "")
        try:
            intent_confidence = float(payload.get("intent_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            intent_confidence = 0.0
        # Phase 2.9A.26+: stable timeline anchors.
        # ``getattr`` is used so legacy tests that build ``SimpleNamespace``
        # messages without the new column do not break.
        conversation_sequence = (
            int(getattr(msg, "conversation_sequence"))
            if getattr(msg, "conversation_sequence", None) is not None
            else None
        )
        reply_to_message_public_id = (
            # Resolved by the route layer (list_messages) after the
            # bulk join below; the service-level _to_detail returns
            # only the internal-id reference.  Front-end uses
            # ``conversation_sequence`` for ordering.
            None
        )
        result = {
            "message_id": msg.public_id,
            "conversation_id": conv_public_id,
            "role": msg.role,
            "message_type": msg.message_type,
            "content": msg.content,
            "payload": normalize_json_object(msg.payload_json),
            "attached_files": cls._attached_files(msg, files),
            "timestamp": msg.created_at.isoformat() + "Z" if msg.created_at else "",
            "route": route,
            "intent": intent,
            "intent_confidence": intent_confidence,
        }
        if conversation_sequence is not None:
            result["conversation_sequence"] = conversation_sequence
        # Internal FK references are surfaced as raw IDs so the
        # front-end can build its own timeline assembly without an
        # extra round-trip.  Public IDs are resolved by the route.
        if getattr(msg, "reply_to_message_id", None) is not None:
            result["reply_to_message_internal_id"] = int(msg.reply_to_message_id)
        return result


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Message 总编排,Phase 2.9A 最大服务):
#
#   入口主路径(用户发送消息):
#     api/v1/messages.py send_message endpoint →
#       → MessageService.send_message(message_payload, user_id, conversation_id)
#         → 5 路由分支(IntentRouter 驱动,F013):
#             1. chat_reply    → ChatLLMService.generate_reply(prompt, chat_context)
#             2. ask_for_files → static default text + 文件缺失列表
#             3. unsupported   → static default text keyed by intent
#             4. clarify       → router 的 reply_message 或 fallback default
#             5. agent_task    → 仅在 files 完备时 → AgentTaskService.create_task()
#         → 落库 user Message + assistant Message(顺序)
#         → 若 agent_task → 同时 Outbox AgentExecutionRequest(给 Worker claim)
#
#   副路径:
#     - 流式聊天:api/v1/messages.py stream endpoint → ChatLLMService.stream_reply
#     - 消息再生:api/v1/messages.py regen → MessageRegenerationService.regenerate
#     - 消息反馈:api/v1/messages.py feedback → MessageFeedbackService.record
#     - 删除/编辑:DELETE/PATCH → 本服务 + message_repository
#
# 关键约束(供开发者速查):
#   - 任何 chat / task 路径都必须先写 user Message,再写 assistant/tasks;
#     这样前端 reducer 永不出现"任务来了但用户消息还没到"的状态;
#   - ChatContext 由 ConversationContextService.build_chat_context(...) 拼;
#   - IntentRouter 是 CHEAP_PROFILE(便宜模型);主聊天模型在 ChatLLMService 内;
#   - agent_task 路径触发的 AgentTaskService 必须把 messages_bind 信息传全,
#     以便 LangGraph 节点能反查对话历史(conversation_message_links);
#   - 5 路由字典(routes)里的 helper 都是同步纯函数,可单独测试;
#   - 文件超大:本文件 ~2972 行,核心是 5 路由 + IntentRouter 集成 + history
#     编排;若新增意图,请扩展 intent_router(IntentRouter 决定),不要在本服务
#     内嵌大型 if/elif 链。
#
#   ⚠️ 重构提示:本文件行数过大,与 message_regeneration / message_feedback
#     部分功能重叠。建议下一步:
#       (a) 把 Intent 解析 + 5 路由分发表抽到 dispatcher 子模块;
#       (b) message_history / timeline 操作抽到 message_query 子模块;
#       (c) 本文件只保留 send_message 主入口与对外契约。
#     注:本注释提示尚未实施,以便后续 PR 跟进。
