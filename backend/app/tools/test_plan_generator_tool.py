"""TestPlanGeneratorTool — generates structured test plan content.

Wires LLMClient + PromptBuilder + ResultParser into a single-pass
generation pipeline.

History:
- Originally implemented a Two-Pass split generator (outline + N
  concurrent batches). F019 removed it — the test plan fits in one
  prompt + one LLM call, and the split was both fragile (truncation
  between batches, batch retries, JSON merge) and unnecessary
  (template fields total ≪ 8k tokens).

All stable capabilities preserved:
- Three-anchor prompt, system_rules, requirement/template/generation-config input
- Single LLM call → raw JSON text
- JSON fence cleanup, bracket extraction, truncation detection, continuation
- Field validation (missing/extra/empty/schema-mismatch, multi-table)
- Control-label sanitisation, section-package construction
- Tool-standard output, agent_events/tool_calls, error codes

F023 (Self-Healing Agent) — schema feedback behaviour:

  This tool's historical behaviour was to re-prompt the LLM ONCE with a
  ``【表头合规性强制要求】`` corrective block whenever
  ``parse_and_validate_json`` rejected the first response for a row-
  schema mismatch.  In F023 that internal retry is **gated** by the
  orchestrator's ``RetryContext.strategy == "schema_feedback"`` flag:

    * Attempt 1 (``retry_context`` is None or ``strategy == ""``):
      trust the LLM's first response; if validation fails, surface
      ``JSON_VALIDATION_FAILED`` to the orchestrator — no internal
      LLM re-call.  The orchestrator then decides to retry.
    * Attempt N (N > 1, ``retry_context.strategy == "schema_feedback"``):
      run the corrective re-prompt path.  On success, return the
      schema-corrected payload as a normal success.  On failure, raise
      ``JSON_VALIDATION_FAILED`` with the *original* validator message
      so the orchestrator's NEXT retry gets the same feedback.

════════════════════════════════════════════════════════════════════════════════
链路位置 (核心工具,TestPlanGenerator + ResultReview + WordExport 三个核心):

  节点 generate_test_plan_node
    → TestPlanGeneratorTool.run(inputs={...}, ctx, retry_context)
      → app.common.prompt_builder 构造 Three-anchor prompt
      → LLMClient.generate(...) 一次性返回 raw JSON
      → app.common.result_parser.cleanup + parse_and_validate_json
      → 写入 context.test_plan_content = {section_package, generated_sections, ...}
      → 自检 schema + field 校验,失败发 JSON_VALIDATION_FAILED

调用合约(供开发者速查):
  - inputs 一般不需要外部传(query 由 TestPlanGeneratorTool 内部从 ctx 取);
  - 失败错误码:JSON_VALIDATION_FAILED / JSON_TRUNCATED / EMPTY_SECTION_CONTENT /
    SCHEMA_HEADER_MISMATCH / MISSING_SECTION 等;
  - 触发 Phase 2.9A.9 Fail-Fast:失败 → generate_test_plan_node 写 last_error +
    task_status=failed → barrier → route_after_generate_test_plan → fail_task;
  - 成功产出 section_package,供 WordExportTool 模板回填导出;
  - 输出经 narrative_composer TestPlanGeneratorContextBuilder 包装,生成
    "测试方案生成完毕"工具叙事。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.common.json_utils import extract_json_from_llm_response
from app.common.result_parser import ResultParser, ResultParseError, ResultSchemaMismatch
from app.context_engine.evidence.requirement_pipeline import (
    RequirementEvidenceBundle,
    RequirementEvidenceChunk,
    RequirementEvidencePreparationError,
    RequirementEvidencePreparer,
    calculate_requirement_direct_limit,
)
from app.fault_injection_gateway import (
    POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
    maybe_inject_fault,
)
from app.integrations.llm_client import LLMClientError, MockLLMClient
from app.tools.base import BaseTool
from app.tools.test_plan_json_contract import (
    build_test_plan_generation_system_prompt,
    build_test_plan_json_contract,
)

logger = logging.getLogger(__name__)


def _retrieval_bundle_records(bundle: Any) -> list[dict[str, Any]]:
    """Render the frozen pre-generation evidence as bounded CE records.

    The records are deliberately data-only, locked to the task snapshot and
    labelled by source so Company guidance cannot masquerade as a project
    fact.  They are not used to trigger another retrieval.
    """
    if not isinstance(bundle, dict) or not bundle.get("frozen"):
        return []
    records: list[dict[str, Any]] = []
    for source_key, source_label in (
        ("project_rag", "project retrieval evidence"),
        ("company_rag", "company guidance evidence"),
    ):
        source = bundle.get(source_key)
        if not isinstance(source, dict):
            continue
        hits = source.get("hits") or []
        for index, hit in enumerate(hits[:9]):
            if not isinstance(hit, dict):
                continue
            content = str(
                hit.get("content") or hit.get("text") or hit.get("snippet") or ""
            ).strip()
            if not content:
                continue
            evidence_id = str(
                hit.get("evidence_id") or hit.get("chunk_id") or hit.get("id") or index
            )
            version_hint = str(hit.get("version_hint") or "").strip()
            date_hint = str(hit.get("date_hint") or "").strip()
            history_hint = ""
            if source_key == "project_rag" and (version_hint or date_hint):
                history_hint = (
                    f"\nDocument version/date hint: "
                    f"{version_hint or 'unspecified'} / {date_hint or 'unspecified'}"
                )
            records.append(
                {
                    "source_ref": f"{source_key}:{evidence_id}"[:256],
                    "title": str(hit.get("title") or hit.get("source") or source_label)[:255],
                    "content": (
                        f"Source: {source_label}\n"
                        f"Evidence ID: {evidence_id}\n"
                        f"{content[:1600]}{history_hint}"
                    ),
                    "locked": True,
                    "retrieval_source": source_key,
                    "retrieval_round": bundle.get("retrieval_round"),
                }
            )
    return records


class TestPlanGeneratorTool(BaseTool):
    name = "TestPlanGeneratorTool"
    description = "根据需求解析结果、模板结构、知识库检索和用户确认生成测试方案内容"

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        use_mock = inputs.get("use_mock", False)

        # ── 1. Read inputs from context ─────────────────────────────
        requirement_analysis = context.requirement_analysis or {}
        template_structure = context.template_structure or {}

        requirement_text = requirement_analysis.get("text_content", "")
        if not requirement_text:
            return self._error(
                "MISSING_REQUIREMENT",
                "缺少需求文档内容，请先完成需求文档解析",
                recoverable=False,
            )

        template_sections = template_structure.get("sections", [])
        generation_config = template_structure.get("generation_config", {})

        if not template_sections:
            return self._error(
                "MISSING_TEMPLATE",
                "缺少模板结构，请先完成模板解析",
                recoverable=False,
            )

        if not generation_config:
            return self._error(
                "MISSING_GENERATION_CONFIG",
                "缺少生成配置，请先完成模板解析",
                recoverable=False,
            )

        ai_fields = generation_config.get("ai_fields", [])
        if not ai_fields:
            return self._error(
                "NO_AI_FIELDS",
                "模板中没有标记为 AI 生成的章节，无需生成",
                recoverable=False,
            )

        # ── 2. Build template_structure dict for PromptBuilder ──────
        await self._progress(context, "正在准备测试方案生成上下文")
        headings = self._extract_headings(template_sections)
        table_fields = self._extract_table_fields(template_sections)

        user_prompt = inputs.get("user_prompt", "") or "请根据需求文档和模板结构生成测试方案内容。"

        # ── 3. Resolve LLM client ──────────────────────────────────
        result_parser = ResultParser()

        from app.context_engine.feature_flags import require_agent_context_migration

        # CE-05 WP-2：任务路径经 context.task_flag_resolver 读 MIG_GENERATE（冻结
        # Manifest）；resolver 缺失/损坏 → fail closed。
        resolver = getattr(context, "task_flag_resolver", None)
        migration_error = require_agent_context_migration(resolver, "MIG_GENERATE")
        if migration_error is not None:
            return self._error(
                migration_error,
                "测试方案生成仅支持 Context Engine；当前任务未冻结 MIG_GENERATE，已阻止旧 Prompt 回退。",
                recoverable=True,
            )
        try:
            prepared_evidence = await self._prepare_requirement_evidence(
                context=context,
                requirement_analysis=requirement_analysis,
            )
        except RequirementEvidencePreparationError as exc:
            logger.warning(
                "TestPlanGeneratorTool: requirement evidence preparation failed | "
                "code=%s | chunk=%s",
                exc.code,
                exc.chunk_ordinal,
            )
            return self._error(
                "REQUIREMENT_EVIDENCE_PREPARATION_FAILED",
                "需求证据未能完成全量分块提取，已阻止生成以避免遗漏需求",
                recoverable=True,
                details={
                    "evidence_error_code": exc.code,
                    "chunk_ordinal": exc.chunk_ordinal,
                },
            )

        requirement_text_for_generation = (
            requirement_text
            if prepared_evidence.mode == "full_text"
            else "\n\n".join(
                str(record.get("content") or "")
                for record in prepared_evidence.records
            )
        )
        ce_task_state_ref = (
            self._build_ce_task_state_ref(
                user_prompt=user_prompt,
                requirement_analysis=requirement_analysis,
                template_structure=template_structure,
                generation_config=generation_config,
                headings=headings,
                table_fields=table_fields,
                prepared_evidence=prepared_evidence,
                retrieval_evidence_bundle=inputs.get("retrieval_evidence_bundle"),
            )
        )
        output_contract = self._build_test_plan_output_contract(generation_config)
        generation_system_prompt = build_test_plan_generation_system_prompt(
            generation_config
        )
        if use_mock:
            raw_json = await MockLLMClient().generate("")
        else:
            from app.llm.task_profiles import TEST_PLAN_RAW_PROFILE
            from app.tools._mig_routing import invoke_via_bridge_or_none

            error, value = await invoke_via_bridge_or_none(
                context=context,
                call_site="test_plan.generate.outline",
                llm_task_profile=TEST_PLAN_RAW_PROFILE,
                current_goal=user_prompt,
                output_contract=output_contract,
                task_state_ref=ce_task_state_ref,
                system_prompt=generation_system_prompt,
            )
            if error is not None:
                return self._error(
                    error,
                    "测试方案生成的 Context Engine 调用失败，未回退到旧 Prompt 链路。",
                    recoverable=True,
                )
            raw_json = value if isinstance(value, str) else __import__("json").dumps(value, ensure_ascii=False)

        # ── 4. Build components ────────────────────────────────────
        # ── 5. Generate (single pass) ───────────────────────────────
        start_time = time.monotonic()
        try:
            await self._progress(context, "正在生成测试方案内容")
            raw_json = maybe_inject_fault(
                POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
                raw_json,
                context={
                    "generation_config": generation_config,
                    "task_id": getattr(context, "task_id", None)
                    or getattr(context, "task_internal_id", None),
                    "tool": self.name,
                },
            )
            elapsed = time.monotonic() - start_time
            logger.info(
                "TestPlanGeneratorTool: generation completed in %.1fs", elapsed
            )

        except LLMClientError as exc:
            elapsed = time.monotonic() - start_time
            error_code = self._classify_model_error(str(exc))
            logger.warning(
                "TestPlanGeneratorTool: LLM 调用失败 | err_code=%s | 耗时=%.1fs | err=%s",
                error_code, elapsed, str(exc)[:200],
            )
            return self._error(
                error_code,
                f"模型调用失败：{exc}",
                recoverable=True,
            )

        # ── 6. Truncation detection & continuation ─────────────────
        # Phase 2.9A.X：F023 整体重试被移除（用户实测：3 次整体重试大概率仍失败）。
        # 截断后的修复路径改走 ResultReviewTool + RepairAgent 定点补缺失章节。
        # A normal plan is still generated in one request.  Only a confirmed
        # truncated response switches this task to the CE outline + batch
        # protocol.  Do not delegate a whole incomplete document to
        # RepairAgent: it is intended for small, post-generation issues.
        truncation_recovery: dict[str, Any] | None = None
        if result_parser.is_json_truncated(raw_json):
            logger.warning(
                "TestPlanGeneratorTool: detected truncated JSON | length=%d | "
                "switching to outline + batch recovery",
                len(raw_json),
            )
            recovered_payload, truncation_recovery = await self._recover_truncated_generation_in_batches(
                context=context,
                user_prompt=user_prompt,
                result_parser=result_parser,
                generation_config=generation_config,
                task_state_ref=ce_task_state_ref,
                raw_length=len(raw_json),
                use_mock=use_mock,
            )
            if recovered_payload is None:
                # A batch failure must not immediately fail the task.  Fall
                # through to the legacy-compatible lenient parser below so
                # that any usable initial fields and explicit missing-field
                # issues can reach the final RepairAgent safety net.
                logger.warning(
                    "TestPlanGeneratorTool: outline + batch recovery failed; "
                    "deferring only unresolved fields to RepairAgent | meta=%s",
                    truncation_recovery,
                )
            else:
                # Re-enter the ordinary parse/package path with a validated
                # full payload.  The partial-to-RepairAgent branch below is
                # therefore only a final safety net, not the main recovery.
                raw_json = json.dumps(recovered_payload, ensure_ascii=False)

        if result_parser.is_json_truncated(raw_json):
            logger.warning(
                "TestPlanGeneratorTool: 检测到JSON截断 | 长度=%d | 尝试续写",
                len(raw_json),
            )
            truncated_after_continuation = True
            # A continuation is another model request.  Do not let this
            # recovery path bypass Context Engine with the old client; the
            # deterministic partial parser below records missing fields for
            # the CE-only regeneration path.

            if truncated_after_continuation:
                # 宽容解析：拿 best-effort partial + schema_issues
                partial_payload, schema_issues = result_parser.parse_json_lenient(
                    raw_json, generation_config
                )
                schema_issues = self._mark_json_truncation_schema_issues(
                    schema_issues,
                    generation_config,
                )
                recovery_meta = self._build_json_truncation_recovery_meta(
                    schema_issues,
                    generation_config,
                    mode="partial" if partial_payload is not None else "complete",
                    raw_length=len(raw_json),
                )
                if truncation_recovery is not None:
                    recovery_meta["outline_batch_attempt"] = truncation_recovery
                if partial_payload is not None:
                    # 部分可恢复 — 包装成 success envelope 让主图继续走 review
                    section_package = result_parser.build_section_package(
                        partial_payload, generation_config
                    )
                    data = {
                        "section_package": section_package,
                        "schema_issues": schema_issues,
                        "generated_sections": len(section_package.get("generated_sections", [])),
                        "kept_sections": len(section_package.get("keep_sections", [])),
                        "manual_sections": len(section_package.get("manual_sections", [])),
                        "total_word_count": len(raw_json),
                        "tables_generated": self._count_tables(section_package),
                        "generation_recovery": recovery_meta,
                    }
                    context.test_plan_content = data
                    logger.info(
                        "TestPlanGeneratorTool: 宽容解析恢复 %d 个章节，"
                        "%d 个缺失章节待 RepairAgent 补生成",
                        len(section_package.get("generated_sections", [])),
                        len(schema_issues),
                    )
                    return self._success(
                        data,
                        (
                            f"测试方案内容部分生成：恢复 "
                            f"{data['generated_sections']} 个章节，"
                            f"{len(schema_issues)} 个缺失章节待修复"
                        ),
                        warnings=[{
                            "code": "JSON_PARTIAL_RECOVERY",
                            "missing_count": len(schema_issues),
                        }],
                    )
                # 宽容解析也完全失败 — 走所有 schema_issues 缺失字段的极端分支
                if not schema_issues:
                    # config 为 None 或 ai_fields 为空 —— 走老路径抛错
                    return self._error(
                        "JSON_TRUNCATED",
                        "LLM 输出 JSON 截断且无法宽容解析",
                        recoverable=True,
                    )
                # 宽容解析返回了 (None, schema_issues) — JSON 完全损坏，
                # 但 schema_issues 已标记所有期望字段缺失 — 由 ResultReviewTool +
                # RepairAgent 全量补生成
                empty_section_package = {
                    "generated_sections": [],
                    "keep_sections": [],
                    "manual_sections": [],
                }
                data = {
                    "section_package": empty_section_package,
                    "schema_issues": schema_issues,
                    "generated_sections": 0,
                    "kept_sections": 0,
                    "manual_sections": 0,
                    "total_word_count": len(raw_json),
                    "tables_generated": 0,
                    "generation_recovery": recovery_meta,
                }
                context.test_plan_content = data
                logger.warning(
                    "TestPlanGeneratorTool: 宽容解析完全失败，"
                    "%d 个缺失章节全部由 RepairAgent 补生成",
                    len(schema_issues),
                )
                return self._success(
                    data,
                    (
                        f"测试方案 JSON 严重损坏，{len(schema_issues)} 个章节"
                        f"全部由 RepairAgent 补生成"
                    ),
                    warnings=[{
                        "code": "JSON_SEVERE_TRUNCATION",
                        "missing_count": len(schema_issues),
                    }],
                )

        # ── 7. Parse & validate ────────────────────────────────────
        try:
            await self._progress(context, "正在校验生成结果")
            payload = result_parser.parse_and_validate_json(
                raw_json, generation_config
            )
        except ResultSchemaMismatch as exc:
            # A structurally-invalid first pass must not be reported as a
            # successful generation and deferred wholesale to RepairAgent.
            # Preserve the usable fields, then regenerate only the invalid
            # template bindings in small CE-only batches.
            logger.info(
                "TestPlanGeneratorTool: schema 不符 → 定向补生成 | "
                "offending=%s",
                list(exc.offending_fields.keys())[:10],
            )
            schema_issues = self._convert_to_schema_issues(exc.offending_fields)
            payload = await self._recover_schema_mismatch(
                context=context,
                raw_json=raw_json,
                result_parser=result_parser,
                generation_config=generation_config,
                schema_issues=schema_issues,
                use_mock=use_mock,
            )
            if payload is None:
                # Do not terminate the task here.  Direct regeneration is an
                # optimization; ResultReview + RepairAgent remain the durable
                # fallback for partial or failed batches.
                deferred_data = context.test_plan_content or {}
                deferred_data["repair_required"] = True
                deferred_data["generation_recovery"] = {
                    "kind": "schema_mismatch",
                    "requires_repair": True,
                    "issue_count": len(deferred_data.get("schema_issues") or schema_issues),
                    "source": "TestPlanGeneratorTool",
                }
                context.test_plan_content = deferred_data
                return self._success(
                    deferred_data,
                    (
                        "测试方案已保留可用章节；剩余结构问题将进入"
                        "ResultReviewTool 和 RepairAgent 定点修复"
                    ),
                    warnings=[{
                        "code": "SCHEMA_RECOVERY_DEFERRED",
                        "issue_count": deferred_data["generation_recovery"]["issue_count"],
                    }],
                )
        except ResultParseError as exc:
            # 真正的 JSON 语法错（不可定点修复）— 走 F023 残留的 JSON 闭合
            # 修复路径（仅处理语法闭合类提示，不含字段白名单 — 整体重试对
            # 内容幻觉仍然帮助有限）。
            logger.warning(
                "TestPlanGeneratorTool: JSON 语法错（非 schema） | err=%s",
                str(exc)[:200],
            )
            syntax_recovery = self._build_json_syntax_recovery_meta(
                raw_json=raw_json,
                exc=exc,
            )
            retry_payload, retry_ok = await self._repair_json_syntax_once(
                exc=exc,
                context=context,
                user_prompt=user_prompt,
                template_generation_config=generation_config,
                task_state_ref=ce_task_state_ref,
                result_parser=result_parser,
            )
            syntax_recovery["repair_attempted"] = True
            syntax_recovery["recovery_round"] = 1
            if retry_ok:
                payload = retry_payload
                syntax_recovery.update({
                    "status": "repaired",
                    "successful_field_count": len(payload or {}),
                })
            else:
                recovered_payload, batch_recovery = await self._recover_truncated_generation_in_batches(
                    context=context,
                    user_prompt=user_prompt,
                    result_parser=result_parser,
                    generation_config=generation_config,
                    task_state_ref=ce_task_state_ref,
                    raw_length=len(raw_json),
                    use_mock=use_mock,
                    recovery_kind="json_syntax_outline_batch",
                    generation_mode_prefix="json_syntax_recovery",
                    recovery_reason=(
                        "The normal full-plan JSON had a non-truncation syntax error."
                    ),
                )
                syntax_recovery["outline_batch_attempt"] = batch_recovery
                syntax_recovery["recovery_round"] = 2
                if recovered_payload is None:
                    syntax_recovery["status"] = "exhausted"
                    logger.error(
                        "TestPlanGeneratorTool: JSON recovery exhausted | "
                        "raw_length=%s | error=%s | batch_failure=%s",
                        syntax_recovery["raw_length"],
                        syntax_recovery.get("json_error"),
                        batch_recovery.get("failure"),
                    )
                    return self._error(
                        "JSON_VALIDATION_FAILED",
                        str(exc),
                        recoverable=False,
                        details={"generation_recovery": syntax_recovery},
                    )
                payload = recovered_payload
                syntax_recovery.update({
                    "status": "recovered",
                    "completed_batches": batch_recovery.get("completed_batches", 0),
                    "successful_field_count": len(payload),
                })
            truncation_recovery = syntax_recovery

        # ── 8. Build section package ───────────────────────────────
        await self._progress(context, "正在整理章节内容")
        section_package = result_parser.build_section_package(
            payload, generation_config
        )

        # ── 9. Summary ─────────────────────────────────────────────
        generated_count = len(section_package.get("generated_sections", []))
        keep_count = len(section_package.get("keep_sections", []))
        manual_count = len(section_package.get("manual_sections", []))

        data = {
            "generated_sections": generated_count,
            "kept_sections": keep_count,
            "manual_sections": manual_count,
            "total_word_count": len(raw_json),
            "tables_generated": self._count_tables(section_package),
            "section_package": section_package,
        }

        if truncation_recovery is not None:
            data["generation_recovery"] = truncation_recovery
        context.test_plan_content = data

        return self._success(
            data,
            f"测试方案内容生成完成：共生成 {generated_count} 个章节、"
            f"保留 {keep_count} 个模板章节、手动 {manual_count} 个章节、"
            f"共约 {data['total_word_count']} 字符",
        )

    # ── helpers ────────────────────────────────────────────────────

    async def _recover_truncated_generation_in_batches(
        self,
        *,
        context: AgentContext,
        user_prompt: str,
        result_parser: ResultParser,
        generation_config: dict,
        task_state_ref: dict[str, Any],
        raw_length: int,
        use_mock: bool,
        recovery_kind: str = "json_truncation_outline_batch",
        generation_mode_prefix: str = "json_truncation_recovery",
        recovery_reason: str = "The normal full-plan JSON was truncated.",
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Recover one failed full-plan result with CE outline and batches.

        This is deliberately not the default generation route.  The first
        response cannot safely be consumed as a complete plan, so this task
        switches to a short consistency outline followed by two-field,
        independently validated batches.
        """
        from app.llm.task_profiles import (
            TEST_PLAN_BATCH_PROFILE,
            TEST_PLAN_OUTLINE_PROFILE,
        )
        from app.tools._mig_routing import invoke_via_bridge_or_none

        ai_fields = [
            field for field in generation_config.get("ai_fields", [])
            if isinstance(field, dict) and str(field.get("field") or "").strip()
        ]
        recovery_meta: dict[str, Any] = {
            "kind": recovery_kind,
            "raw_length": raw_length,
            "batch_size": 2,
            "field_count": len(ai_fields),
            "completed_batches": 0,
        }
        if not ai_fields:
            recovery_meta["failure"] = "no_ai_fields"
            return None, recovery_meta

        outline_state = dict(task_state_ref)
        outline_state["generation_mode"] = f"{generation_mode_prefix}_outline"
        outline_goal = (
            f"{user_prompt}\n\n"
            f"{recovery_reason} Produce only a concise "
            "consistency outline for the subsequent small section batches; do "
            "not produce the test-plan chapters themselves."
        )
        outline_contract = self._build_outline_output_contract(ai_fields)
        if use_mock:
            outline = {}
        else:
            error, value = await invoke_via_bridge_or_none(
                context=context,
                call_site="test_plan.generate.outline",
                llm_task_profile=TEST_PLAN_OUTLINE_PROFILE,
                current_goal=outline_goal,
                output_contract=outline_contract,
                task_state_ref=outline_state,
            )
            if error is not None or value is None:
                recovery_meta["failure"] = error or "outline_empty"
                return None, recovery_meta
            try:
                outline = result_parser.parse_json(
                    value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                )
                recovery_meta["outline_source"] = "llm"
            except ResultParseError:
                # The outline improves cross-batch consistency but must not
                # make a recoverable content-generation failure terminal.
                outline = {}
                recovery_meta["outline_source"] = "deterministic_empty"

        batch_payloads: list[dict[str, Any]] = []
        for start in range(0, len(ai_fields), 2):
            batch_fields = ai_fields[start : start + 2]
            batch_config = self._generation_config_subset(
                generation_config,
                batch_fields,
            )
            field_names = [str(field["field"]) for field in batch_fields]
            batch_state = dict(task_state_ref)
            batch_state.update({
                "generation_mode": f"{generation_mode_prefix}_batch",
                "batch_index": (start // 2) + 1,
                "batch_count": (len(ai_fields) + 1) // 2,
                "batch_fields": field_names,
                "consistency_outline": outline,
            })
            batch_goal = (
                f"{user_prompt}\n\n"
                "Generate only this recovery batch of test-plan fields: "
                + ", ".join(field_names)
                + ". Follow the server JSON contract exactly."
            )
            if use_mock:
                recovery_meta["failure"] = "mock_batch_recovery_not_supported"
                return None, recovery_meta
            error, value = await invoke_via_bridge_or_none(
                context=context,
                call_site="test_plan.generate.batch",
                llm_task_profile=TEST_PLAN_BATCH_PROFILE,
                current_goal=batch_goal,
                output_contract=self._build_test_plan_output_contract(batch_config),
                task_state_ref=batch_state,
                system_prompt=build_test_plan_generation_system_prompt(batch_config),
            )
            if error is not None or value is None:
                recovery_meta.update({
                    "failure": error or "batch_empty",
                    "failed_batch": recovery_meta["completed_batches"] + 1,
                    "failed_fields": field_names,
                })
                return None, recovery_meta
            try:
                raw_batch = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                batch_payload = result_parser.parse_and_validate_json(raw_batch, batch_config)
            except (ResultParseError, ResultSchemaMismatch) as exc:
                recovery_meta.update({
                    "failure": "batch_contract_invalid",
                    "failed_batch": recovery_meta["completed_batches"] + 1,
                    "failed_fields": field_names,
                    "validation_error": str(exc)[:500],
                })
                return None, recovery_meta
            batch_payloads.append(batch_payload)
            recovery_meta["completed_batches"] += 1

        try:
            payload = result_parser.merge_batch_payloads(batch_payloads, generation_config)
            validation = result_parser.validate_json_payload(payload, generation_config)
            if not validation.passed:
                recovery_meta.update({
                    "failure": "merged_payload_invalid",
                    "missing_fields": validation.missing_fields,
                    "empty_fields": validation.empty_fields,
                    "schema_mismatch_fields": validation.schema_mismatch_fields,
                })
                return None, recovery_meta
        except ResultParseError as exc:
            recovery_meta.update({
                "failure": "batch_merge_failed",
                "validation_error": str(exc)[:500],
            })
            return None, recovery_meta

        recovery_meta["status"] = "recovered"
        return payload, recovery_meta

    @staticmethod
    def _generation_config_subset(
        generation_config: dict,
        batch_fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Preserve template bindings while narrowing validation to one batch."""
        fields = [dict(field) for field in batch_fields]
        field_names = {str(field["field"]) for field in fields}
        return {
            "ai_fields": fields,
            "keep_sections": generation_config.get("keep_sections", []),
            "manual_sections": generation_config.get("manual_sections", []),
            "section_bindings": [
                binding
                for binding in generation_config.get("section_bindings", [])
                if isinstance(binding, dict)
                and str(binding.get("field") or "") in field_names
            ],
        }

    @staticmethod
    def _build_outline_output_contract(ai_fields: list[dict[str, Any]]) -> str:
        field_names = [str(field["field"]) for field in ai_fields]
        return (
            "Return exactly one strict JSON object and no prose or Markdown. "
            "It must contain only terminology (object), cross_cutting (array), "
            "and chapter_summaries (object). chapter_summaries must use exactly "
            "these literal keys and a short string value for each: "
            + json.dumps(field_names, ensure_ascii=False)
        )

    @staticmethod
    def _build_ce_task_state_ref(
        *,
        user_prompt: str,
        requirement_analysis: dict,
        template_structure: dict,
        generation_config: dict,
        headings: list[str],
        table_fields: list[str],
        prepared_evidence: RequirementEvidenceBundle,
        retrieval_evidence_bundle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a compact CE task_state_ref for test-plan generation."""
        ai_fields = generation_config.get("ai_fields", [])
        return {
            "test_plan_generation_instruction": user_prompt,
            "ai_fields": ai_fields if isinstance(ai_fields, list) else [],
            "template_headings": headings,
            "table_fields": table_fields,
            "conversation_policy": {
                "recent_complete_turns": 6,
                "include_summary": False,
                "protect_recent_turns": True,
            },
            "context_evidence": {
                "evidence_mode": prepared_evidence.mode,
                "coverage_manifest": prepared_evidence.coverage_manifest,
                "parsed_documents": (
                    list(prepared_evidence.records)
                    + _retrieval_bundle_records(retrieval_evidence_bundle)
                ),
                "template_sections": [
                    {
                        "source_ref": "template_structure",
                        "title": str(
                            template_structure.get("template_name")
                            or (
                                headings[0]
                                if headings
                                else "parsed template sections"
                            )
                        ),
                        "content": _render_template_evidence(
                            template_structure.get("sections") or []
                        ),
                        "locked": True,
                    }
                ],
            },
        }

    @staticmethod
    def _build_test_plan_output_contract(generation_config: dict) -> str:
        """Build the trusted, template-derived JSON contract for this call.

        The field names and table headers are data from the selected template,
        but the requirement to emit exactly that shape is a server-owned call
        contract.  It must therefore travel through ``ContextRequest`` rather
        than the untrusted Task State renderer.
        """
        raw_fields = generation_config.get("ai_fields") or []
        return build_test_plan_json_contract(
            [raw for raw in raw_fields if isinstance(raw, dict)]
        )

    async def _prepare_requirement_evidence(
        self,
        *,
        context: AgentContext,
        requirement_analysis: dict[str, Any],
    ) -> RequirementEvidenceBundle:
        direct_limit_tokens = await self._resolve_requirement_direct_limit(context)

        async def extract(chunk: RequirementEvidenceChunk) -> str:
            prompt = (
                f"来源: {chunk.source_ref}\n"
                f"分块: {chunk.ordinal}/{chunk.total}\n"
                f"字符范围: {chunk.start_char}:{chunk.end_char}\n"
                f"SHA256: {chunk.sha256}\n\n"
                f"{chunk.source_text}"
            )
            from app.llm.task_profiles import REQUIREMENT_EVIDENCE_EXTRACT_PROFILE

            from app.tools._mig_routing import invoke_via_bridge_or_none

            error, value = await invoke_via_bridge_or_none(
                context=context,
                call_site="test_plan.requirement.extract",
                llm_task_profile=REQUIREMENT_EVIDENCE_EXTRACT_PROFILE,
                current_goal=prompt,
                output_contract="markdown_requirement_ledger",
                task_state_ref={
                    "source_ref": chunk.source_ref,
                    "chunk_id": chunk.chunk_id,
                    "chunk_ordinal": chunk.ordinal,
                    "chunk_total": chunk.total,
                    "start_char": chunk.start_char,
                    "end_char": chunk.end_char,
                    "source_sha256": chunk.sha256,
                },
            )
            if error is not None or value is None:
                return ""
            return str(value).strip()

        return await RequirementEvidencePreparer().prepare(
            requirement_analysis,
            extractor=extract,
            direct_limit_tokens=direct_limit_tokens,
        )

    @staticmethod
    async def _resolve_requirement_direct_limit(context: AgentContext) -> int | None:
        """Resolve a model-aware raw-evidence allowance when settings exist.

        ``None`` deliberately means "use the preparer's compatibility
        default" for isolated/mocked tool calls that do not carry a settings
        service.  A configured model whose window metadata is absent uses the
        Context Engine's conservative unknown-window calculation.
        """
        settings_service = getattr(context, "settings_service", None)
        if settings_service is None or not hasattr(settings_service, "get_model_settings"):
            return None
        user_id = (
            getattr(context, "user_internal_id", 0)
            or getattr(context, "user_id", None)
        )
        try:
            settings = await settings_service.get_model_settings(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TestPlanGeneratorTool: model window lookup failed; using "
                "conservative unknown-window evidence budget | error=%s",
                type(exc).__name__,
            )
            settings = {}
        window = settings.get("context_window_tokens") if isinstance(settings, dict) else None
        try:
            model_context_window = int(window) if window else None
        except (TypeError, ValueError):
            model_context_window = None
        return calculate_requirement_direct_limit(model_context_window)

    @staticmethod
    def _extract_headings(sections: list[dict]) -> list[str]:
        """Extract heading strings from template sections."""
        result = []
        for s in sections:
            if isinstance(s, dict):
                title = s.get("title", "")
                if title:
                    result.append(title)
                for child in s.get("children", []) or []:
                    if isinstance(child, dict) and child.get("title"):
                        result.append(f"  {child['title']}")
        return result

    @staticmethod
    def _extract_table_fields(sections: list[dict]) -> list[str]:
        """Extract table field descriptions from template sections."""
        result = []
        for s in sections:
            if isinstance(s, dict):
                schemas = s.get("table_schemas", [])
                if isinstance(schemas, list):
                    for schema in schemas:
                        if isinstance(schema, dict):
                            headers = schema.get("headers", [])
                            if headers:
                                result.append(
                                    f"{s.get('title', '')} 表格字段："
                                    + " | ".join(str(h) for h in headers)
                                )
        return result

    @staticmethod
    async def _resolve_llm_config(context: AgentContext) -> LLMConfigProvider:
        """Resolve LLM configuration from the user's settings_service (DB).

        F015 — per-user model configuration.  ``build_llm_config_provider``
        returns either a real ``LLMConfigProvider`` (the user has a
        ``model_configs`` row) or an ``LLMNotConfiguredMarker`` (no
        row).  In the marker case, ``LLMClient._do_chat_completion``
        detects ``is_unconfigured=True`` and raises
        ``LLMClientError("LLM_NOT_CONFIGURED: ...")`` which our caller
        maps to a ``MODEL_CONFIG_ERROR`` tool failure.

        The legacy ``_EnvConfig`` fallback has been removed entirely.
        """

        if getattr(context, "settings_service", None) is None:
            # No settings_service attached to context — return marker so
            # the upstream LLM call produces a clear, user-facing error.
            return LLMNotConfiguredMarker()

        user_internal_id = getattr(context, "user_internal_id", 0) or 0
        if user_internal_id <= 0:
            logger.warning(
                "TestPlanGeneratorTool: context.user_internal_id is "
                "missing or zero; returning unconfigured marker"
            )
            return LLMNotConfiguredMarker()

        provider = await context.settings_service.build_llm_config_provider(
            user_id=user_internal_id
        )
        if isinstance(provider, LLMNotConfiguredMarker):
            logger.info(
                "TestPlanGeneratorTool: user_id=%s has no model "
                "configuration; LLM call will surface '未配置' error",
                user_internal_id,
            )
            return provider

        logger.info(
            "TestPlanGeneratorTool: using DB-backed LLM config for "
            "user_id=%s (model=%s)",
            user_internal_id,
            provider.model_name,
        )
        return provider

    @staticmethod
    def _classify_model_error(message: str) -> str:
        """Classify an LLM error message into a standard error code."""
        if "LLM_NOT_CONFIGURED" in message or "未配置" in message:
            return "MODEL_CONFIG_ERROR"
        if "超时" in message or "timeout" in message.lower():
            return "MODEL_TIMEOUT"
        if "连接" in message or "connection" in message.lower():
            return "MODEL_CONNECTION_ERROR"
        if "HTTP" in message or "状态码" in message or "status" in message.lower():
            return "MODEL_STATUS_ERROR"
        return "MODEL_UNKNOWN_ERROR"

    @staticmethod
    def _count_tables(section_package: dict) -> int:
        generated = section_package.get("generated_sections", [])
        count = 0
        for sec in generated:
            if isinstance(sec, dict):
                schemas = sec.get("table_schemas", [])
                if isinstance(schemas, list) and schemas:
                    count += len(schemas)
        return count

    @staticmethod
    def _convert_to_schema_issues(
        offending_fields: dict,
    ) -> list[dict[str, Any]]:
        """将 ``ResultSchemaMismatch.offending_fields`` 转成统一 schema_issues 列表。

        输入：``{field_name: {kind, field, ...}}``
        输出：``[{kind, field, severity, message}, ...]``
        """
        issues: list[dict[str, Any]] = []
        for field_name, info in offending_fields.items():
            if not isinstance(info, dict):
                # 兜底：info 可能是 str（兼容未来扩展）
                issues.append({
                    "kind": "schema_mismatch",
                    "field": field_name,
                    "severity": "block",
                    "message": str(info),
                })
                continue
            kind = info.get("kind", "schema_mismatch")
            messages = info.get("messages") or []
            message = (
                "；".join(messages) if messages
                else f"字段「{field_name}」存在 {kind} 问题"
            )
            issues.append({
                "kind": kind,
                "field": field_name,
                "severity": "block",
                "message": message,
            })
        return issues

    async def _recover_schema_mismatch(
        self,
        *,
        context: AgentContext,
        raw_json: str,
        result_parser: ResultParser,
        generation_config: dict,
        schema_issues: list[dict[str, Any]],
        use_mock: bool = False,
    ) -> dict[str, Any] | None:
        """Regenerate invalid bindings before the primary generation succeeds.

        ``TestPlanRegenTool`` is the single CE-only implementation of the
        strict per-field contract.  Reusing it here ensures a first-pass
        shape error and a later repair use identical evidence, prompting and
        validation rules.  Returning ``None`` defers unresolved fields to the
        normal ResultReview + RepairAgent path instead of publishing a false
        all-clear generation result.
        """
        from app.tools.test_plan_regen_tool import TestPlanRegenTool

        try:
            initial_payload = result_parser.sanitize_payload(
                result_parser.parse_json(raw_json)
            )
            section_package = result_parser.build_section_package(
                initial_payload,
                generation_config,
            )
        except ResultParseError as exc:
            logger.warning(
                "TestPlanGeneratorTool: schema recovery cannot prepare "
                "partial package | error=%s",
                str(exc)[:200],
            )
            return None

        ai_field_lookup = self._ai_field_lookup(generation_config)
        targeted_issues: list[dict[str, Any]] = []
        target_section_ids: list[str] = []
        for issue in schema_issues:
            if not isinstance(issue, dict):
                continue
            field = str(issue.get("field") or "")
            binding = ai_field_lookup.get(field) or {}
            section_id = str(
                binding.get("section_id") or binding.get("field") or ""
            )
            if not section_id:
                # An extra response-only field is removed by
                # build_section_package; it is not a section to regenerate.
                continue
            enriched = dict(issue)
            enriched["section_id"] = section_id
            targeted_issues.append(enriched)
            if section_id not in target_section_ids:
                target_section_ids.append(section_id)

        if not target_section_ids:
            validation = result_parser.validate_json_payload(
                section_package.get("payload") or {},
                generation_config,
            )
            if validation.passed:
                return result_parser._ordered_payload(
                    section_package.get("payload") or {},
                    validation.required_fields,
                )
            logger.warning(
                "TestPlanGeneratorTool: schema recovery has no targetable "
                "fields | issues=%s",
                [issue.get("field") for issue in schema_issues if isinstance(issue, dict)],
            )
            return None

        data = {
            "section_package": section_package,
            "schema_issues": targeted_issues,
            "generated_sections": len(section_package.get("generated_sections", [])),
            "kept_sections": len(section_package.get("keep_sections", [])),
            "manual_sections": len(section_package.get("manual_sections", [])),
            "total_word_count": len(raw_json),
            "tables_generated": self._count_tables(section_package),
        }
        context.test_plan_content = data

        try:
            envelope = await TestPlanRegenTool().run(
                {
                    "section_ids": target_section_ids,
                    "issues": targeted_issues,
                    "generation_config_subset": generation_config,
                    "bulk_repair": True,
                    "use_mock": use_mock,
                },
                context,
            )
        except Exception as exc:  # noqa: BLE001 - fail primary generation closed
            logger.warning(
                "TestPlanGeneratorTool: targeted schema recovery raised | "
                "error=%s",
                type(exc).__name__,
            )
            return None
        result_data = envelope.get("data") if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or not envelope.get("success")
            or not isinstance(result_data, dict)
            or result_data.get("partial_success")
            or result_data.get("unresolved_section_ids")
        ):
            logger.warning(
                "TestPlanGeneratorTool: targeted schema recovery failed | "
                "success=%s partial=%s unresolved=%s",
                envelope.get("success") if isinstance(envelope, dict) else None,
                result_data.get("partial_success") if isinstance(result_data, dict) else None,
                result_data.get("unresolved_section_ids") if isinstance(result_data, dict) else None,
            )
            return None

        repaired_content = context.test_plan_content or {}
        repaired_package = repaired_content.get("section_package") or {}
        repaired_payload = repaired_package.get("payload") or {}
        validation = result_parser.validate_json_payload(
            repaired_payload,
            generation_config,
        )
        if not validation.passed:
            logger.warning(
                "TestPlanGeneratorTool: targeted schema recovery validation "
                "still failed | missing=%s empty=%s schema=%s",
                validation.missing_fields,
                validation.empty_fields,
                validation.schema_mismatch_fields,
            )
            return None

        return result_parser._ordered_payload(
            repaired_payload,
            validation.required_fields,
        )

    @staticmethod
    def _ai_field_lookup(generation_config: dict | None) -> dict[str, dict]:
        ai_fields = (
            generation_config.get("ai_fields")
            if isinstance(generation_config, dict)
            else []
        ) or []
        lookup: dict[str, dict] = {}
        for entry in ai_fields:
            if not isinstance(entry, dict):
                continue
            for key in (
                entry.get("field"),
                entry.get("section_id"),
                entry.get("title"),
            ):
                if key:
                    lookup.setdefault(str(key), entry)
        return lookup

    @classmethod
    def _mark_json_truncation_schema_issues(
        cls,
        schema_issues: list[dict[str, Any]],
        generation_config: dict | None,
    ) -> list[dict[str, Any]]:
        """Mark lenient-parser issues as symptoms of a truncated JSON output."""
        lookup = cls._ai_field_lookup(generation_config)
        marked: list[dict[str, Any]] = []
        for issue in schema_issues or []:
            if not isinstance(issue, dict):
                continue
            field = str(issue.get("field") or "")
            binding = lookup.get(field) or {}
            item = dict(issue)
            item["source_error"] = "json_truncated"
            item["recovery_strategy"] = "bulk_regenerate_missing_sections"
            if binding.get("section_id"):
                item.setdefault("section_id", binding.get("section_id"))
            if binding.get("title"):
                item.setdefault("title", binding.get("title"))
            marked.append(item)
        return marked

    @classmethod
    def _build_json_truncation_recovery_meta(
        cls,
        schema_issues: list[dict[str, Any]],
        generation_config: dict | None,
        *,
        mode: str,
        raw_length: int,
    ) -> dict[str, Any]:
        lookup = cls._ai_field_lookup(generation_config)
        missing_fields: list[str] = []
        missing_section_ids: list[str] = []
        for issue in schema_issues or []:
            if not isinstance(issue, dict):
                continue
            field = str(issue.get("field") or "")
            if field and field not in missing_fields:
                missing_fields.append(field)
            binding = lookup.get(field) or {}
            section_id = str(
                issue.get("section_id") or binding.get("section_id") or field
            )
            if section_id and section_id not in missing_section_ids:
                missing_section_ids.append(section_id)
        return {
            "kind": "json_truncated",
            "mode": mode,
            "source": "TestPlanGeneratorTool",
            "raw_length": raw_length,
            "missing_count": len(missing_fields),
            "missing_fields": missing_fields,
            "missing_section_ids": missing_section_ids,
            "requires_bulk_repair": True,
        }

    @staticmethod
    def _build_json_syntax_recovery_meta(
        *, raw_json: str, exc: ResultParseError
    ) -> dict[str, Any]:
        """Keep diagnostics useful without retaining the raw model response."""
        import re

        error_text = str(exc)
        match = re.search(r"第\s*(\d+)\s*行，第\s*(\d+)\s*列", error_text)
        position = (
            {"line": int(match.group(1)), "column": int(match.group(2))}
            if match
            else None
        )
        return {
            "kind": "json_syntax_outline_batch",
            "source": "TestPlanGeneratorTool",
            "raw_length": len(raw_json),
            "json_error": error_text[:500],
            "json_error_position": position,
            "finish_reason": "unavailable",
            "recovery_round": 0,
            "repair_attempted": False,
        }

    async def _repair_json_syntax_once(
        self,
        *,
        exc: ResultParseError,
        context: AgentContext,
        user_prompt: str,
        template_generation_config: dict,
        task_state_ref: dict[str, Any],
        result_parser: ResultParser,
    ) -> tuple[dict | None, bool]:
        """Re-prompt LLM ONCE 仅针对 JSON 语法错（不含 schema 字段修复）。

        Phase 2.9A.X：F023 拆分后的"语法闭合修复"路径。
        * 表头/字段 schema 不符（``ResultSchemaMismatch``）已不再走这里，
          改走 envelope.data.schema_issues 透传
        * 仅处理 **JSON 语法错**（花括号/方括号/引号未闭合、转义错等）—
          这些错误整体重试对模型帮助有限（用户实测 3 次仍失败），所以
          仅注入极简 ``【JSON 闭合要求】`` 提示，不带字段白名单

        This is a tool-owned, single low-temperature attempt. It always runs
        before the two-field batch fallback and never uses the legacy prompt
        chain.
        """
        # 通用 JSON 闭合提示 — 不含字段白名单
        augmented_user_prompt = (
            user_prompt
            + "\n\n【JSON 闭合要求 — 上次响应 JSON 不合法，请严格闭合花括号/方括号/引号】\n"
            + "只输出一个严格合法的 JSON 对象，所有 ``{`` 必须配对 ``}``，"
            + "所有 ``[`` 必须配对 ``]``，所有字符串值必须用双引号闭合，"
            + "禁止末尾省略闭合符号。"
        )

        logger.warning(
            "TestPlanGeneratorTool: JSON syntax repair attempt | err=%s",
            str(exc)[:120],
        )
        from app.llm.task_profiles import TEST_PLAN_JSON_REPAIR_RAW_PROFILE
        from app.tools._mig_routing import invoke_via_bridge_or_none

        retry_state_ref = dict(task_state_ref)
        retry_state_ref["generation_mode"] = "json_syntax_repair"
        retry_state_ref["json_retry"] = {
            "reason": str(exc)[:500],
            "attempt": 1,
        }
        error, value = await invoke_via_bridge_or_none(
            context=context,
            call_site="test_plan.generate.outline",
            llm_task_profile=TEST_PLAN_JSON_REPAIR_RAW_PROFILE,
            current_goal=augmented_user_prompt,
            output_contract=self._build_test_plan_output_contract(
                template_generation_config
            ),
            task_state_ref=retry_state_ref,
            system_prompt=build_test_plan_generation_system_prompt(
                template_generation_config
            ),
        )
        if error is not None:
            logger.warning(
                "TestPlanGeneratorTool: JSON retry Context Engine call failed | code=%s",
                error,
            )
            return None, False
        raw_json_2 = value if isinstance(value, str) else __import__("json").dumps(value, ensure_ascii=False)

        try:
            payload_2 = result_parser.parse_and_validate_json(
                raw_json_2, template_generation_config,
            )
        except ResultParseError as exc:
            logger.warning(
                "TestPlanGeneratorTool: JSON 闭合修复后仍校验失败 | err=%s",
                str(exc)[:200],
            )
            return None, False

        logger.info(
            "TestPlanGeneratorTool: JSON syntax repair validated",
        )
        return payload_2, True

    async def _maybe_retry_with_json_syntax_fix(self, **kwargs) -> tuple[dict | None, bool]:
        """Compatibility shim for orchestrator-era retry callers."""
        retry_context = kwargs.pop("retry_context", None)
        if retry_context is None or retry_context.strategy != "schema_feedback":
            return None, False
        return await self._repair_json_syntax_once(**kwargs)

    @staticmethod
    def _extract_offending_fields_from_error(
        error_message: str, template_generation_config: dict,
    ) -> dict[str, list[str]]:
        """Identify which ``ai_fields[*].field`` had a row-schema mismatch.

        Walks the template's ``ai_fields`` and matches against the
        validator's error text ("section_X" prefix is present in the
        mismatch description).  Returns ``{field: [headers...]}`` for
        every field whose row-schema failed; empty dict means nothing
        row-related to fix.
        """
        ai_fields = template_generation_config.get("ai_fields", [])
        if not isinstance(ai_fields, list):
            return {}

        offending: dict[str, list[str]] = {}
        for item in ai_fields:
            if not isinstance(item, dict):
                continue
            field_name = str(item.get("field", "")).strip()
            if not field_name:
                continue
            if field_name in error_message:
                schemas = item.get("table_schemas", [])
                if not isinstance(schemas, list) or not schemas:
                    continue
                # Only retry when row-schema is involved (any schema
                # with explicit headers).
                for schema in schemas:
                    if (
                        isinstance(schema, dict)
                        and isinstance(schema.get("headers"), list)
                        and schema.get("headers")
                    ):
                        offending[field_name] = [str(h) for h in schema["headers"]]
                        break
        return offending

    @staticmethod
    def _build_corrective_schema_block(
        offending_fields: dict[str, list[str]],
        template_generation_config: dict,
    ) -> str:
        """Render a per-field enforcement block for the corrective prompt."""
        ai_fields = template_generation_config.get("ai_fields", [])
        title_by_field: dict[str, str] = {}
        if isinstance(ai_fields, list):
            for item in ai_fields:
                if isinstance(item, dict):
                    fn = str(item.get("field", "")).strip()
                    title = str(item.get("title", "")).strip()
                    if fn and title:
                        title_by_field[fn] = title

        lines: list[str] = []
        for field, headers in offending_fields.items():
            title = title_by_field.get(field, field)
            lines.append(f"- 字段 ``{field}``（章节：{title}）")
            lines.append(
                "  表头列表（必须且只能使用这 "
                f"{len(headers)} 个键，键名一字不差）："
                + "、".join(f"``{h}``" for h in headers)
            )
            lines.append("  禁止新增列、禁止删除列、禁止改名列名或简写。")
            lines.append("  每一行的 keys 集合必须等于上述列表。")
        return "\n".join(lines)


# ── F015: Legacy _EnvConfig removed ────────────────────────────────
#
def _render_template_evidence(sections: list[dict]) -> str:
    """Render the parsed template tree as bounded, non-instructional evidence."""
    lines: list[str] = []

    def visit(nodes: list[dict], depth: int = 0) -> None:
        for section in nodes:
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or "untitled section").strip()
            section_id = str(section.get("section_id") or "").strip()
            field = str(section.get("field") or section.get("suggested_field") or "").strip()
            mode = str(section.get("mode") or "").strip()
            details = [f"title={title}"]
            if section_id:
                details.append(f"section_id={section_id}")
            if field:
                details.append(f"field={field}")
            if mode:
                details.append(f"mode={mode}")
            schemas = section.get("table_schemas")
            if isinstance(schemas, list):
                headers: list[str] = []
                for schema in schemas[:5]:
                    if isinstance(schema, dict) and isinstance(schema.get("headers"), list):
                        headers.extend(str(header) for header in schema["headers"][:20])
                if headers:
                    details.append("table_headers=" + ", ".join(headers[:40]))
            lines.append("  " * min(depth, 8) + "- " + "; ".join(details))
            children = section.get("children")
            if isinstance(children, list):
                visit(children, depth + 1)

    visit(sections if isinstance(sections, list) else [])
    return "\n".join(lines)


# The previous ``_EnvConfig`` fallback that read ``LLM_API_URL`` /
# ``LLM_MODEL_NAME`` / ``DASHSCOPE_API_KEY`` from the process
# environment has been removed.  Model configuration is now strictly
# per-user and DB-backed — see ``_resolve_llm_config`` above.
