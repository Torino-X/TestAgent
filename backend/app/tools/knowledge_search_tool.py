"""KnowledgeSearchTool — searches the company knowledge base API.

Real backend integration (F017):

  * Reads per-user ``KnowledgeConfig`` via ``KnowledgeConfigService``.
  * Builds a generic read-only Company RAG client with the decrypted
    API key.
  * Calls the chunk-retrieval endpoint and writes the normalised
    result into ``context.knowledge_search_result``.
  * Never raises into the Agent flow.  On failure, emits a structured
    recoverable error so the orchestrator can offer
    ``fallback_general`` mode (per Constitution 13.7).

Key invariants (do not break):

  * API key never leaves the backend process.
  * Plaintext key is never written to ``context.knowledge_search_result``.
  * On KB unavailable, the main workflow continues — only an event is
    recorded for downstream fallback decisions.

════════════════════════════════════════════════════════════════════════════════
链路位置 (Phase 2.1 起就被 8-Tool 链路调用):

  节点 search_knowledge_node
    → KnowledgeSearchTool.run(inputs={"query": ..., "knowledge_ids": [...],
                                       "top_k": N, "similarity_threshold": float,
                                       "_simulate_failure": bool})
      → resolve KnowledgeConfig(decrypt API key from DB)
      → 调 CompanyRagClient.retrieve_chunks(...) → KnowledgeRetrievalService
        (rerank + chunk 解析)
      → 把 hits / confidence / similarity 写入 context.knowledge_search_result

Phase 2.9A.X 后的失败降级语义(本会话着重强调):

  * 配置类错误(KNOWLEDGE_NOT_CONFIGURED / KNOWLEDGE_SESSION_MISSING /
    KNOWLEDGE_API_KEY_INVALID)→ 改为 _success(degraded) 而非 _error,
    任务继续推进(避免"知识库未配置"就让测试任务整个失败);
  * 上游 KB 故障(api_unavailable)→ _success(degraded_payload),不触发 RepairAgent;
  * 模拟失败(_simulate_failure)→ 与 api_unavailable 行为一致。

链路输出的 knowledge_search_result 会被 narrative_composer 的
KnowledgeSearchContextBuilder 消费,生成"知识库检索"工具叙事。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.core.crypto import CryptoError, decrypt_api_key
from app.repositories.knowledge_config_repository import KnowledgeConfigRepository
from app.services.knowledge_retrieval_service import KnowledgeRetrievalService
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class KnowledgeSearchTool(BaseTool):
    name = "KnowledgeSearchTool"
    description = "调用公司知识库 API，检索历史测试方案、规范和术语"

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        query = (inputs.get("query") or "").strip()
        logger.info(
            "COMPANY_RAG_QUERY_START | tool=KnowledgeSearchTool | user_id=%s | task_id=%s | query_len=%d",
            getattr(context, "user_internal_id", None),
            getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
            len(query),
        )
        await self._progress(context, "正在构建知识库查询")
        if not query:
            # No query provided by the orchestrator. This is a soft
            # "skip" (the calling chain did not want to invoke KB) and
            # MUST NOT surface as a failed tool_call — the
            # orchestrator does not abort on KnowledgeSearchTool
            # failure (it's not in _CORE_TOOLS), so a failure would
            # only show up as a confusing red ring in the UI.
            data = self._build_disabled_payload(query="", reason="query_missing")
            context.knowledge_search_result = data
            logger.warning(
                "COMPANY_RAG_QUERY_SKIPPED | reason=query_missing | user_id=%s | task_id=%s",
                getattr(context, "user_internal_id", None),
                getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
            )
            return self._success(
                data,
                "知识库检索已跳过：调用方未提供查询关键词。",
            )

        # Optional explicit failure simulation (used by tests)
        if inputs.get("_simulate_failure"):
            # Phase 2.9A.X: 与 api_unavailable 对齐 — 走 _success(degraded)
            # 而非 _error,避免触发 Repair Agent / task_failed 链路。
            context.knowledge_search_result = self._build_degraded_payload(
                query=query,
                reason="simulate_failure",
                error_code="KNOWLEDGE_API_UNAVAILABLE",
                error_message="simulated failure",
            )
            logger.warning(
                "COMPANY_RAG_QUERY_FAILED | reason=simulate_failure | user_id=%s | task_id=%s",
                getattr(context, "user_internal_id", None),
                getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
            )
            return self._success(
                context.knowledge_search_result,
                "知识库检索失败(模拟),已自动降级为空结果。",
                warnings=["模拟失败", "已降级为通用生成模式(fallback_general)"],
            )

        session: AsyncSession | None = getattr(context, "session", None)
        if session is None:
            context.knowledge_search_result = self._build_degraded_payload(
                query=query,
                reason="session_missing",
                error_code="KNOWLEDGE_SESSION_MISSING",
                error_message="DB session missing",
            )
            logger.error(
                "COMPANY_RAG_QUERY_FAILED | reason=session_missing | user_id=%s | task_id=%s",
                getattr(context, "user_internal_id", None),
                getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
            )
            # BUG FIX 2026-08-18 (方案 2):配置类错误(未配置/session 缺失/
            # key 无效)不再返回 _error()。用户设计明确:知识库未配置/上游
            # 失败都不导致整个任务失败。改为 _success(degraded),与
            # api_unavailable 对齐,任务继续基于本地上下文生成。
            return self._success(
                context.knowledge_search_result,
                "知识库检索降级完成：Agent 上下文缺少 DB session，无法读取知识库配置。",
                warnings=["session 缺失，按 KB 未配置处理"],
            )

        # 1. Resolve config + decrypt key
        repo = KnowledgeConfigRepository(session)
        row = await repo.get_for_user(int(getattr(context, "user_internal_id", 0) or 0))
        if row is None or not row.api_key_encrypted:
            # Per-user row + system-shared row both absent
            context.knowledge_search_result = self._build_degraded_payload(
                query=query,
                reason="not_configured",
                error_code="KNOWLEDGE_NOT_CONFIGURED",
                error_message="Knowledge base API key is not configured.",
            )
            logger.warning(
                "COMPANY_RAG_QUERY_FAILED | reason=not_configured | user_id=%s | task_id=%s | query=%s",
                getattr(context, "user_internal_id", None),
                getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
                query[:120],
            )
            # BUG FIX 2026-08-18 (方案 2):未配置 → 降级 success,不失败任务。
            return self._success(
                context.knowledge_search_result,
                "知识库检索降级完成：尚未配置公司知识库 API Key。",
                warnings=["未找到有效的知识库配置"],
            )
        try:
            api_key = decrypt_api_key(row.api_key_encrypted)
        except CryptoError:
            logger.warning(
                "KnowledgeSearchTool.run: API Key 解密失败 | user=%s",
                getattr(context, "user_id", None),
            )
            # BUG FIX 2026-08-18 (方案 2):key 无效 → 降级 success,不失败任务。
            context.knowledge_search_result = self._build_degraded_payload(
                query=query,
                reason="api_key_invalid",
                error_code="KNOWLEDGE_API_KEY_INVALID",
                error_message="API Key 解密失败",
            )
            return self._success(
                context.knowledge_search_result,
                "知识库检索降级完成：存储的知识库 API Key 无法解密。",
                warnings=["API Key 解密失败"],
            )

        # 2026-07 起 KB 在生成测试方案时始终启用，test_plan_generation_enabled
        # 这个开关不再被读取（保留字段以兼容旧数据 / 前端配置 UI），忽略之。

        if getattr(row, "test_plan_generation_enabled", True) is False:
            data = self._build_disabled_payload(query=query, reason="toggled_off")
            context.knowledge_search_result = data
            logger.info(
                "COMPANY_RAG_QUERY_SKIPPED | reason=toggled_off | user_id=%s | task_id=%s",
                getattr(context, "user_internal_id", None),
                getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
            )
            return self._success(data, "知识库检索已跳过：知识库生成开关已关闭。")

        # 2. Call retrieval via the service so we share normalisation
        retrieval = KnowledgeRetrievalService(session)
        effective_top_k = int(inputs.get("top_k") or row.top_k or 5)
        effective_threshold = float(
            inputs.get("similarity_threshold") or row.similarity_threshold or 0.35
        )

        await self._progress(context, "正在查询知识库")
        started_at = time.monotonic()
        result = await retrieval.retrieve(
            user_internal_id=int(getattr(context, "user_internal_id", 0) or 0),
            query=query,
            top_k=effective_top_k,
            similarity_threshold=effective_threshold,
            retrieve_strategy=int(inputs.get("retrieve_strategy") or row.retrieve_strategy or 3),
            enable_rerank_model=bool(
                inputs.get("enable_rerank_model", row.enable_rerank_model)
            ),
            rerank_model=inputs.get("rerank_model") or row.rerank_model or "bge-reranker-v2-m3",
            labels=inputs.get("labels") or [],
        )

        if not result.success:
            # Phase 2.9A.X bug fix: KB 上游失败(KB 限流 / 参数非法 /
            # 服务暂时不可用)不应让整个测试任务失败。
            # docstring 第 8-12 行已经承诺 "Never raises into the Agent
            # flow. On failure, emits a structured recoverable error" ——
            # 但实际之前用 ``_error()`` 返回会让 v3 主图把这次检索记为
            # 工具失败,触发 Repair Agent / Narrative fallback,甚至整个
            # task 失败。这里把 KB 链路自身导致的失败转成 ``_success(degraded
            # payload)``,与 docstring / 设计预期保持一致:
            # * ``knowledge_search_result`` 已经写入 degraded(保留事实)
            # * tool 返回 success 但带 ``degraded=True``,让下游 Generator
            #   按"未命中"继续生成
            # * 不会触发 ``_core_failed`` 分支 / RepairAgent / task_failed
            # 配置类错误(KNOWLEDGE_NOT_CONFIGURED / KNOWLEDGE_SESSION_MISSING
            # / KNOWLEDGE_API_KEY_INVALID)保留 ``_error()``,因为这些是
            # 用户必须手动修复的真实问题,不能降级为 silent success。
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            context.knowledge_search_result = self._build_degraded_payload(
                query=query,
                reason="api_unavailable",
                error_code=str(result.error_code or "KNOWLEDGE_API_UNAVAILABLE"),
                error_message=str(result.error_message or "Knowledge retrieval failed."),
            )
            logger.warning(
                "COMPANY_RAG_QUERY_FAILED | reason=api_unavailable | user_id=%s | task_id=%s | "
                "error_code=%s | elapsed_ms=%d",
                getattr(context, "user_internal_id", None),
                getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
                result.error_code,
                elapsed_ms,
            )
            return self._success(
                context.knowledge_search_result,
                (
                    f"知识库检索失败({result.error_code or 'KNOWLEDGE_API_UNAVAILABLE'}),"
                    f"已自动降级为空结果;测试方案生成将按无知识库参考继续。"
                ),
                warnings=[
                    f"知识库错误码:{result.error_code}",
                    "已降级为通用生成模式(fallback_general)",
                ],
            )

        # 3. Translate standardised hits into the legacy shape so
        # downstream tools / UI continue to work.
        await self._progress(context, "正在整理检索结果")
        data = self._translate_hits(result, query)
        context.knowledge_search_result = data
        logger.info(
            "COMPANY_RAG_QUERY_SUCCESS | user_id=%s | task_id=%s | hit_count=%d | "
            "confidence=%s | elapsed_ms=%s | docs=%s",
            getattr(context, "user_internal_id", None),
            getattr(context, "task_id", None) or getattr(context, "task_internal_id", None),
            int(data.get("hit_count") or 0),
            data.get("confidence"),
            data.get("elapsed_ms"),
            [item.get("name") for item in data.get("similar_projects", [])[:5]],
        )
        return self._success(
            data,
            f"知识库检索完成：命中 {len(result.hits)} 条，置信度 {result.confidence}",
        )

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _translate_hits(result: Any, query: str) -> dict[str, Any]:
        similar_projects: list[dict[str, Any]] = []
        standards: list[str] = []
        terms: list[str] = []
        for hit in result.hits:
            if hit.doc_name and hit.doc_name not in standards:
                standards.append(hit.doc_name)
            content_snippet = (hit.content or "").strip()
            if content_snippet and len(terms) < 12:
                first_line = content_snippet.splitlines()[0][:40]
                if first_line and first_line not in terms:
                    terms.append(first_line)
            similar_projects.append(
                {
                    "name": hit.doc_name or hit.chunk_title or hit.chunk_id or "(未知)",
                    "doc_id": hit.doc_id,
                    "chunk_id": hit.chunk_id,
                    "knowledge_id": hit.knowledge_id,
                    "similarity": float(hit.score) if isinstance(hit.score, (int, float)) else None,
                    "snippet": content_snippet[:240],
                }
            )
        return {
            "similar_projects": similar_projects,
            "standards": standards,
            "terms": terms,
            "query": query,
            "hit_count": len(result.hits),
            "confidence": result.confidence,
            "elapsed_ms": result.elapsed_ms,
            "direct_answer_eligible": result.direct_answer_eligible,
            "degraded": False,
            "source": "company_rag",
        }

    @staticmethod
    def _build_disabled_payload(
        query: str, reason: str = "disabled_by_config"
    ) -> dict[str, Any]:
        return {
            "similar_projects": [],
            "standards": [],
            "terms": [],
            "query": query,
            "hit_count": 0,
            "confidence": "low",
            "elapsed_ms": 0,
            "direct_answer_eligible": False,
            "disabled_by_config": True,
            "skip_reason": reason,
            "degraded": True,
            "source": "company_rag",
        }

    @staticmethod
    def _build_degraded_payload(
        *,
        query: str,
        reason: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        return {
            "similar_projects": [],
            "standards": [],
            "terms": [],
            "hits": [],
            "query": query,
            "hit_count": 0,
            "confidence": "low",
            "elapsed_ms": 0,
            "direct_answer_eligible": False,
            "degraded": True,
            "source": "company_rag",
            "skip_reason": reason,
            "error_code": error_code,
            "error_message": error_message,
        }
