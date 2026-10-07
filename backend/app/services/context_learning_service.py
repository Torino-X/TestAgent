"""ContextLearningService — Post-turn Context Learning Pipeline。

WP-BE-06/08：assistant response 持久化并发送到终端后，soft-async 执行：
    用户消息 → LLM 分类（USER_MEMORY / PROJECT_RULE / EPHEMERAL）
    → ContextSecurityService（Secret/PII/Injection/Owner）
    → User Memory 写入（scope=user） / Project Rule 写入（conversation scope）

硬约束：
- 用户可见回复不能等待它完成（调用方 soft-async，此处失败只吞日志）。
- 禁止把 assistant 自己生成的建议当 User Memory（只处理 user message）。
- Secret/PII/Injection 拒绝写入。
- User Memory：scope_type='user'，workspace_key=None。
- Project Rule：workspace_key=conversation:{public_id}。

分类规则（确定性提示词 + LLM 三分类）：
    USER_MEMORY  ：用户长期偏好/事实（"以后都用中文回答"）
    PROJECT_RULE ：当前项目硬性约束（"接口统一用 v3"）
    EPHEMERAL    ：临时任务/一次性指令（"今天先测试登录"）→ 丢弃
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 确定性 Secret 检测（防把凭据写入 Memory；与 PII 正交）
_SECRET_PATTERNS = [
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret)\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),  # 常见 sk- 前缀 key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"(?:password|passwd|pwd|私钥)\s*[=:：]\s*\S+", re.IGNORECASE),
    re.compile(r"密码\s*(?:是|为|[=:：])\s*\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:login|登录)(?:密码|口令)\s*[=:：]\s*\S+", re.IGNORECASE),
]

# 显式长期记忆触发词（确定性策略：即使 LLM 分类异常，命中显式词也提升为 active）
_EXPLICIT_REMEMBER_MARKERS = (
    "记住",
    "以后",
    "我通常",
    "我的偏好",
    "请记住",
    "remember",
)


class ContextSecurityError(Exception):
    """内容未通过安全校验（拒绝写入）。"""

    def __init__(self, reason: str, code: str = "context.memory.security_denied") -> None:
        self.reason = reason
        self.code = code
        super().__init__(reason)


def detect_secret(text: str) -> bool:
    """确定性检测明文 Secret（不写入 Memory）。"""
    return any(p.search(text or "") for p in _SECRET_PATTERNS)


def contains_pii(text: str) -> bool:
    """复用 PIIRedactor 检测。"""
    from app.context_engine.security.pii import PIIRedactor

    return PIIRedactor().contains_pii(text)


def is_explicit_remember(text: str) -> bool:
    """是否显式长期记忆触发（命中标记 → 高置信 active）。"""
    lowered = (text or "").lower()
    return any(marker in text or marker.lower() in lowered for marker in _EXPLICIT_REMEMBER_MARKERS)


def _mentions_credential_material(text: str) -> bool:
    """Reject requests about credential material even when no secret value is present."""
    return bool(re.search(
        r"(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|password|passwd|cookie|private[_ -]?key|"
        r"密码|口令|私钥|令牌)",
        text or "",
        flags=re.IGNORECASE,
    ))


class ContextSecurityService:
    """Memory/Rule 写入前的安全校验。

    WP-BE-06 §十六：至少覆盖 Secret / PII / Prompt Injection / Owner Scope。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def check_for_write(self, content: str) -> None:
        """Secret / PII 拒绝写入（deterministic，不依赖 LLM）。"""
        if detect_secret(content):
            raise ContextSecurityError("检测到疑似密钥/凭据，拒绝写入记忆")
        if contains_pii(content):
            raise ContextSecurityError("检测到个人敏感信息，拒绝写入记忆")

    def check_owner(self, *, user_id: int, owner_user_id: int) -> None:
        """Owner Scope：写入者必须与 owner 一致。"""
        if int(user_id) != int(owner_user_id):
            raise ContextSecurityError("跨用户写入拒绝", code="context.memory.owner_mismatch")


class ContextLearningService:
    """Post-turn 上下文学习：用户消息 → 分类 → 安全校验 → 持久化。"""

    def __init__(
        self,
        session: AsyncSession,
        llm_invoker=None,
        *,
        session_factory=None,
        llm_client=None,
    ) -> None:
        self._session = session
        self._invoker = llm_invoker
        self._security = ContextSecurityService(session)
        # 生产注入的 DB session 工厂。bridge/compose 路径的 SourceAdapter（如
        # CONVERSATION）经 runtime_context.session_factory() 读 session；缺省时
        # 分类只能走确定性兜底（LLM 不可用语义），不阻塞主流程。
        self._session_factory = session_factory
        # bridge 的 ContextAwareLLMInvoker 经 runtime_context.llm_client 取真实
        # LLM 客户端；缺省时 invoker 的 provider 调用失败 → 确定性兜底。
        self._llm_client = llm_client

    async def learn_from_user_message(
        self,
        *,
        user_message: str,
        user_internal_id: int,
        conversation_public_id: str | None = None,
        conversation_internal_id: int | None = None,
        recent_context: str | None = None,
        allow_project_rule: bool = True,
        project_public_id: str | None = None,
        project_memory_enabled: bool = True,
    ) -> dict[str, Any]:
        """soft-async 学习入口。绝不抛异常（调用方不必 try）。"""
        from app.context_engine.feature_flags import get_context_engine_flags

        if not get_context_engine_flags().context_memory_write_enabled:
            return {
                "persisted": 0,
                "error": None,
                "skipped": "memory_write_disabled",
            }
        try:
            items = await self._classify(
                user_message,
                recent_context,
                user_internal_id=user_internal_id,
                conversation_internal_id=conversation_internal_id,
            )
            return await self._persist_classified(
                items=items,
                user_internal_id=user_internal_id,
                conversation_public_id=conversation_public_id,
                conversation_internal_id=conversation_internal_id,
                allow_project_rule=allow_project_rule,
                project_public_id=project_public_id,
                project_memory_enabled=project_memory_enabled,
            )
        except Exception as exc:  # noqa: BLE001 — soft-async，失败只记录
            logger.warning(
                "ContextLearningService failed | user=%d | err=%s",
                user_internal_id, type(exc).__name__,
            )
            return {"persisted": 0, "error": type(exc).__name__}

    # ── 分类 ──────────────────────────────────────────────────────

    async def _classify(
        self,
        user_message: str,
        recent_context: str | None,
        *,
        user_internal_id: int | None = None,
        conversation_internal_id: int | None = None,
    ) -> list[dict[str, str]]:
        """调用 LLM 三分类；LLM 不可用 → 确定性规则兜底。"""
        # An explicit “remember” is direct user consent for durable memory.
        # Do not let a probabilistic classifier silently downgrade it to a
        # conversation-only rule or EPHEMERAL content.
        if is_explicit_remember(user_message):
            if _mentions_credential_material(user_message):
                logger.warning(
                    "CONTEXT_MEMORY_EXPLICIT_REMEMBER_REJECTED | reason=credential_material"
                )
                return []
            logger.info(
                "CONTEXT_MEMORY_EXPLICIT_REMEMBER | action=deterministic_user_memory"
            )
            return self._deterministic_classify(user_message)

        try:
            if self._invoker is not None and getattr(self._invoker, "available", False):
                from app.llm.task_profiles import MEMORY_EXTRACT_PROFILE

                payload = user_message
                bres = await self._invoker.generate(
                    user_id=int(user_internal_id or 0),
                    call_site="memory.extract.user",
                    llm_task_profile=MEMORY_EXTRACT_PROFILE,
                    current_goal=payload,
                    output_contract="json",
                    user_content=payload,
                    conversation_id=conversation_internal_id,
                    runtime_context=self._build_runtime_context(
                        user_internal_id=user_internal_id,
                        conversation_internal_id=conversation_internal_id,
                    ),
                )
                if bres is not None and bres.value is not None:
                    parsed = _extract_items(bres.value)
                    if parsed:
                        return parsed
        except Exception as exc:  # noqa: BLE001 — LLM 失败走确定性兜底
            # 记录安全字段（code/stage/type），不泄露 Prompt / Memory / Secret。
            err = getattr(exc, "error", None)
            if err is not None:
                logger.warning(
                    "ContextLearning classify failed | stage=%s | code=%s | type=%s",
                    getattr(err, "stage", None).value
                    if getattr(err, "stage", None) is not None else None,
                    getattr(err, "code", None),
                    type(exc).__name__,
                )
            else:
                logger.warning(
                    "ContextLearning classify failed | type=%s | detail=%s",
                    type(exc).__name__,
                    str(exc)[:160],
                )

        # 确定性兜底（LLM 不可用/失败）
        return self._deterministic_classify(user_message)

    def _build_runtime_context(
        self,
        *,
        user_internal_id: int | None,
        conversation_internal_id: int | None,
    ):
        """构造 bridge 路径的轻量 runtime_context。

        与 MessageService._build_title_runtime_context / IntentRouter
        _build_runtime_context 同语义：提供 ``session_factory`` + ``llm_client``
        满足 SourceAdapter 读 session（CONVERSATION 等）与 invoker 的 provider
        调用，避免 ``NoneType.session_factory`` / ``NoneType.llm_client`` 报错。
        session_factory 未注入时返回 None（分类退化为确定性兜底）。
        """
        if self._session_factory is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            session_factory=self._session_factory,
            user_internal_id=int(user_internal_id or 0),
            conversation_internal_id=conversation_internal_id,
            llm_client=self._llm_client,
        )

    def _deterministic_classify(self, user_message: str) -> list[dict[str, str]]:
        """无 LLM 时的确定性分类（只处理显式信号）。"""
        items: list[dict[str, str]] = []
        if is_explicit_remember(user_message):
            items.append(
                {
                    "category": "USER_MEMORY",
                    "content": user_message,
                    "reason": "显式长期记忆触发词",
                }
            )
        elif any(marker in user_message for marker in ("项目决定", "项目约束", "已确认", "确认采用")):
            items.append({
                "category": "PROJECT_RULE",
                "key": deterministic_rule_key(user_message),
                "content": user_message,
                "reason": "显式项目决定或约束",
            })
        return items

    # ── 持久化 ────────────────────────────────────────────────────

    async def _persist_classified(
        self,
        *,
        items: list[dict[str, str]],
        user_internal_id: int,
        conversation_public_id: str | None,
        conversation_internal_id: int | None,
        allow_project_rule: bool,
        project_public_id: str | None = None,
        project_memory_enabled: bool = True,
    ) -> dict[str, Any]:
        persisted = 0
        project_facts: list[dict[str, str]] = []
        for item in list(items or [])[:5]:
            category = str(item.get("category", "EPHEMERAL")).strip().upper()
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            if category == "EPHEMERAL":
                continue
            # 安全校验（Secret/PII）→ 拒绝
            try:
                self._security.check_for_write(content)
            except ContextSecurityError:
                logger.warning("ContextLearning 安全拒绝 | category=%s", category)
                continue

            if category == "USER_MEMORY":
                ok = await self._persist_user_memory(
                    content=content,
                    user_internal_id=user_internal_id,
                    explicit=is_explicit_remember(content),
                    memory_key=str(item.get("key", "")) or None,
                )
                persisted += int(ok)
            elif category == "PROJECT_RULE" and allow_project_rule and conversation_public_id:
                if project_public_id and project_memory_enabled:
                    project_facts.append({
                        "content": content,
                        "key": str(item.get("key", "")),
                    })
                else:
                    ok = await self._persist_project_rule(
                        content=content,
                        user_internal_id=user_internal_id,
                        conversation_public_id=conversation_public_id,
                        rule_key=str(item.get("key", "")) or None,
                    )
                    persisted += int(ok)
            else:
                logger.debug(
                    "ContextLearning 跳过 | category=%s | need_conv=%s",
                    category, bool(conversation_public_id),
                )
        if project_facts and project_public_id and conversation_public_id:
            try:
                from app.services.project_memory_service import ProjectMemoryService

                result = await ProjectMemoryService(self._session).record_chat_facts(
                    user_id=user_internal_id,
                    project_public_id=project_public_id,
                    conversation_public_id=conversation_public_id,
                    facts=project_facts,
                )
                persisted += int(result.get("persisted") or 0)
            except Exception as exc:  # noqa: BLE001 — post-turn is fail-open
                logger.warning("ContextLearning persist project memory failed: %s", exc)
        try:
            await self._session.commit()
        except Exception as exc:  # noqa: BLE001
            await self._session.rollback()
            logger.warning("ContextLearning commit failed: %s", exc)
        return {"persisted": persisted, "error": None}

    async def _persist_user_memory(
        self,
        *,
        content: str,
        user_internal_id: int,
        explicit: bool,
        memory_key: str | None = None,
    ) -> bool:
        """User Memory：scope='user'，workspace_key=None。"""
        from app.context_engine.memory.memory_service import MemoryService

        try:
            service = MemoryService(self._session)
            identity = (memory_key or "").strip() or deterministic_user_memory_key(content)
            result = await service.create_candidate(
                user_id=user_internal_id,
                scope_type="user",
                workspace_key=None,
                agent_type=None,
                memory_type="preference",
                content=content,
                title=content[:50],
                dedupe_key=identity or None,
            )
            # 显式长期记忆 + 无冲突 → active（auto activate flag 门控）
            from app.context_engine.feature_flags import get_context_engine_flags

            flags = get_context_engine_flags()
            if (
                explicit
                and flags.context_memory_auto_activate_enabled
                and flags.context_memory_write_enabled
            ):
                await service.activate(
                    user_id=user_internal_id,
                    memory_public_id=result["memory_public_id"],
                )
                await self._session.flush()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("ContextLearning persist user memory failed: %s", exc)
            return False

    async def _persist_project_rule(
        self,
        *,
        content: str,
        user_internal_id: int,
        conversation_public_id: str,
        rule_key: str | None = None,
    ) -> bool:
        """Project Rule：conversation:{public_id} scope。复用 WorkspaceInstruction。

        WP-BE-08 Supersede Lifecycle：
        - semantic identity = rule_key（stable 主题键，如 api_version）；
        - DEDUPE：同 key + 同 content_hash → 幂等跳过；
        - SUPERSEDE：同 key + 不同 content_hash（且非同一规则重复）→
          旧 active 标记 superseded + 新规则 active + supersedes_instruction_id；
        - 无 rule_key → 用 deterministic_rule_key() 派生；仍空 → content hash 键
          （保守，不误 supersede 无关规则）。
        全部在单事务内（调用方统一 commit）。
        """
        try:
            from app.context_engine.memory.memory_service import content_hash
            from app.models.context_engine import ContextWorkspaceInstruction
            from app.repositories.context_engine_repositories import (
                WorkspaceInstructionRepository,
            )
            from app.utils.ids import generate_public_id

            repo = WorkspaceInstructionRepository(self._session)
            workspace_key = f"conversation:{conversation_public_id}"
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            new_hash = content_hash(content)

            # semantic identity：LLM key > deterministic key > content-hash 兜底
            identity = (rule_key or "").strip()
            if not identity:
                identity = deterministic_rule_key(content)
            identity = identity or new_hash

            # 查当前 workspace 的 active 规则
            existing_active = await repo.list_effective_for_workspace(
                user_internal_id, workspace_key, now=now, status="active"
            )

            # DEDUPE：同 identity + 同 content_hash → 幂等跳过
            for rule in existing_active:
                if (
                    getattr(rule, "instruction_key", None) == identity
                    and getattr(rule, "content_hash", None) == new_hash
                ):
                    return True

            # SUPERSEDE：同 identity + 不同 content_hash → 旧 active 失效
            superseded_old = None
            for rule in existing_active:
                if (
                    getattr(rule, "instruction_key", None) == identity
                    and getattr(rule, "content_hash", None) != new_hash
                ):
                    superseded_old = rule
                    break
            if superseded_old is not None:
                superseded_old.status = "superseded"
                superseded_old.effective_to = now
                await self._session.flush()

            # 新规则 active（单事务：supersede 旧 + 建新 一起提交）
            rule = ContextWorkspaceInstruction(
                public_id=generate_public_id("wi_"),
                user_id=user_internal_id,
                workspace_key=workspace_key,
                instruction_key=identity,
                category="constraint",
                title=content[:50],
                content=content,
                priority=5,
                status="active",  # 自动提取默认 active（高 authority 约束）
                source_type="auto_extract",
                source_public_id=f"conv:{conversation_public_id}",
                content_hash=new_hash,
                idempotency_key=generate_public_id("idem_"),
                version=(superseded_old.version + 1) if superseded_old else 1,
                supersedes_instruction_id=superseded_old.id if superseded_old else None,
                created_by_user_id=user_internal_id,
                effective_from=now,
                created_at=now,
                updated_at=now,
            )
            from app.repositories.base import ensure_model_id

            await ensure_model_id(self._session, ContextWorkspaceInstruction, rule)
            self._session.add(rule)
            await self._session.flush()
            return True
        except Exception as exc:  # noqa: BLE001
            # WP-BE-08 TEST-07：新规则创建失败 → 恢复旧规则的 superseded 标记，
            # 不留半完成 supersede（事务一致性：旧+新要么一起生效，要么都不生效）。
            try:
                if superseded_old is not None:
                    superseded_old.status = "active"
                    superseded_old.effective_to = None
                    await self._session.flush()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("ContextLearning persist project rule failed: %s", exc)
            return False


def _extract_items(value: Any) -> list[dict[str, str]]:
    """从 LLM 输出提取 items 列表（兼容 dict / str JSON / list）。

    WP-BE-08：透传 PROJECT_RULE 的稳定主题键 ``key``（supersede identity）。
    """
    def _norm(i):
        return {
            "category": str(i.get("category", "EPHEMERAL")),
            "key": str(i.get("key", "")) if i.get("key") else "",
            "content": str(i.get("content", "")),
            "reason": str(i.get("reason", "")),
        }

    if isinstance(value, dict):
        items = value.get("items") or []
        if isinstance(items, list):
            return [_norm(i) for i in items if isinstance(i, dict)]
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return _extract_items(parsed)
            if isinstance(parsed, list):
                return [_norm(i) for i in parsed if isinstance(i, dict)]
        except json.JSONDecodeError:
            pass
    return []


def deterministic_rule_key(content: str) -> str:
    """无 LLM 提供 key 时的确定性规则主题键（轻量、非 contains 模糊匹配）。

    只处理明确主题词：从 content 提取首个非停用词主题段（如 "接口"/"版本"），
    归一化为小写 snake。无法提取时返回空串（调用方回落为 content hash 键，
    避免错误归类为 supersede）。
    """
    import re as _re

    text = (content or "").strip().lower()
    if not text:
        return ""
    # 常见明确主题前缀 → 稳定键（示例级，非穷举）
    for keyword, key in (
        ("接口", "api"),
        ("版本", "api_version"),
        ("浏览器", "browser"),
        ("ie", "browser_ie"),
        ("chrome", "browser_chrome"),
        ("编码", "encoding"),
        ("字符集", "encoding"),
        ("灾备", "disaster_recovery"),
        ("章节", "chapter"),
        ("模板", "template"),
    ):
        if keyword in text:
            return key
    # 兜底：取前两个非停用汉字作为保守主题（不匹配则空）
    return ""


def deterministic_user_memory_key(content: str) -> str:
    """Return a conservative stable identity for explicit user preferences.

    The learning model normally supplies this identity.  Explicit "remember"
    requests deliberately bypass that model, so this helper recognises only a
    narrow, common preference shape rather than merging unrelated memories.
    Unknown preferences remain independent.
    """
    text = (content or "").strip().lower()
    is_test_plan = "测试方案" in text or "test plan" in text
    is_chapter = "每章" in text or "每个章节" in text or "chapter" in text
    is_length = "字数" in text or "字" in text or "word" in text
    if is_test_plan and is_chapter and is_length:
        return "test_plan_chapter_min_words"
    return ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


__all__ = [
    "ContextLearningService",
    "ContextSecurityService",
    "ContextSecurityError",
    "detect_secret",
    "contains_pii",
    "is_explicit_remember",
    "deterministic_user_memory_key",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Post-turn Context Learning):
#
#   链路 (assistant response 已落库):
#     用户消息 + assistant response → MessageService 落库 →
#       → 软异步 fire-and-forget:
#         → ContextLearningService.learn_from_turn(message_pair)
#           → 用户消息 → LLM 分类(USER_MEMORY / PROJECT_RULE / EPHEMERAL)
#           → ContextSecurityService 过滤(secret / PII / injection / owner)
#           → scope=user → User Memory 行;scope=conversation → Project Rule 行
#
# 关键约束(供开发者速查):
#   - 必须 soft-async,不能阻塞 assistant 落库回包;
#   - 分类 LLM 调用用 CHEAP_PROFILE(便宜模型),不能拖累主任务;
#   - 任何写 user memory 前必须过 ContextSecurityService;
#   - 用户删除/重置记忆时,本服务在 erase_user_memory(...) 里反向清理;
#   - 失败仅日志(LearnAttempt 表),不允许 message 写入失败连带回滚。
