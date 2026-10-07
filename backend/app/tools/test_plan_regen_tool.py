"""TestPlanRegenTool — targeted regeneration of failed sections.

F025 — invoked by the orchestrator's review-regen loop when
``ResultReviewTool`` reports ``level == "failed"``.  Unlike
``TestPlanGeneratorTool`` (which generates the whole plan in one
prompt), this tool regenerates **only** the sections whose issues have
``severity == "block"``, in a focused prompt that:

  * feeds back the exact rule violation (and its evidence) for each
    section, so the LLM knows what to fix;
  * tells the LLM to output a strict JSON object containing ONLY the
    targeted fields (no other fields, no extra sections);
  * re-validates the LLM response against the per-section subset of
    ``generation_config``.

The tool then splices the regenerated sections back into
``ctx.test_plan_content["section_package"]["generated_sections"]``
(in-place replacement) and emits a payload the orchestrator can drop
back into ``ctx``.

The orchestrator owns the loop budget (``MAX_REVIEW_LOOPS = 3``); this
tool only does one focused regeneration per call.

════════════════════════════════════════════════════════════════════════════════
链路位置 (Phase 2.1 修复式 regen 路径):

  节点 regenerate_sections_node (review 节点失败级联)
    → TestPlanRegenTool.run(inputs={"target_sections": [...], "violations": [...]})
      → 把 violation 拼成 focused prompt,只发被 block 的 section
      → LLM 严格 JSON 输出,只覆盖 target_sections
      → in-place 替换 ctx.test_plan_content 里的对应 section
      → 返回修复后的 section payload

调用合约(供开发者速查):
  - 每次调用仅做一轮 focused regen(不做多轮 retry);
  - loop 计数由 review_loop_count 在 orchestrator 端维护,MAX_REVIEW_LOOPS=3;
  - 失败错误码:JSON_VALIDATION_FAILED / EMPTY_TARGET / SCHEMA_MISMATCH;
  - 公司运行日志 2026-08-17 修过 _build_prompt 静态方法内误用 self 的 bug。

由 v3 graph 中:
  - regenerate_sections_node(LEGACY regen 路径,severity=block 才走);
  - repair_subgraph_node(Phase 2.4 RepairAgent 接管时不走本工具,改走智能修复)。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.common.json_utils import extract_json_from_llm_response
from app.common.result_parser import ResultParser, ResultParseError
from app.agent_runtime.repair.issue_parser import coerce_evidence_text
from app.integrations.llm_client import LLMClientError, MockLLMClient
from app.tools.base import BaseTool
from app.tools.test_plan_json_contract import (
    build_test_plan_generation_system_prompt,
    build_test_plan_json_contract,
    table_value_shape_description,
)

logger = logging.getLogger(__name__)

# Cap the number of sections we ask the LLM to fix in a single call.
# 3 is empirically safe: short prompt, single LLM response, easy to
# validate.  Larger N risks the same truncation/multi-section errors
# we just spent F019 cleaning up.
MAX_REGEN_BATCH = 3


class TestPlanRegenTool(BaseTool):
    name = "TestPlanRegenTool"
    description = (
        "针对 ResultReviewTool 反馈的违规章节，按 rule 重写其正文/表格内容，"
        "输出严格的 JSON 对象"
    )

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        section_ids: list[str] = list(inputs.get("section_ids") or [])
        issues: list[dict] = list(inputs.get("issues") or [])
        gen_config_subset: dict = inputs.get("generation_config_subset") or {}
        use_mock: bool = bool(inputs.get("use_mock", False))
        bulk_repair: bool = bool(
            inputs.get("bulk_repair")
            or inputs.get("recovery_mode") == "json_truncated"
        )

        # ── 1. Validate inputs ────────────────────────────────────
        if not section_ids:
            return self._error(
                "REGEN_NO_SECTIONS",
                "TestPlanRegenTool: 缺少 section_ids",
                recoverable=False,
            )
        if not isinstance(issues, list) or not issues:
            return self._error(
                "REGEN_NO_ISSUES",
                "TestPlanRegenTool: 缺少 issues",
                recoverable=False,
            )

        # Phase 2.9A.X bug fix: cfg_subset self-healing.
        # RepairAgent LLM 构造 cfg_subset 时经常丢字段 / 把 section_id 错用为 field,
        # 导致 _find_config_entry 反复 MISS → REGEN_NOTHING_TARGETABLE。
        # 这里从 context.template_structure.generation_config.ai_fields 重新
        # 构造 cfg_subset,确保 cfg_subset 至少包含所有 section_ids 对应的 ai_field
        # entry(优先 RepairAgent 传入的;找不到或缺失时用 ctx 里的真实 ai_fields)。
        # The RepairAgent may provide a non-empty but *partial* subset when a
        # review has multiple target sections.  Treat the template structure as
        # authoritative and put its matching entries first, while retaining any
        # request-only entries as a compatibility fallback.  Previously we only
        # rebuilt an entirely empty subset, so one omitted target silently became
        # unrepairable and consumed the repair-loop budget.
        rebuilt = self._rebuild_cfg_subset(context, section_ids, inputs)
        rebuilt_fields = rebuilt.get("ai_fields") if isinstance(rebuilt, dict) else []
        supplied_fields = (
            gen_config_subset.get("ai_fields")
            if isinstance(gen_config_subset, dict)
            else []
        )
        if not isinstance(rebuilt_fields, list):
            rebuilt_fields = []
        if not isinstance(supplied_fields, list):
            supplied_fields = []

        if rebuilt_fields:
            authoritative_keys = {
                str(entry.get("field") or entry.get("section_id") or entry.get("title") or "")
                for entry in rebuilt_fields
                if isinstance(entry, dict)
            }
            fallback_fields = [
                entry
                for entry in supplied_fields
                if isinstance(entry, dict)
                and str(entry.get("field") or entry.get("section_id") or entry.get("title") or "")
                not in authoritative_keys
            ]
            gen_config_subset = {
                **(gen_config_subset if isinstance(gen_config_subset, dict) else {}),
                **rebuilt,
                "ai_fields": [*rebuilt_fields, *fallback_fields],
            }
            logger.warning(
                "TestPlanRegenTool: cfg_subset reconciled from template | "
                "section_ids=%s | authoritative_ai_fields=%d | supplied_ai_fields=%d",
                list(section_ids),
                len(rebuilt_fields),
                len(supplied_fields),
            )
        elif not supplied_fields:
            return self._error(
                "REGEN_NO_CONFIG",
                "TestPlanRegenTool: 缺少 generation_config_subset",
                recoverable=False,
            )

        # ── 2. Pull existing sections from ctx ────────────────────
        existing_package = (context.test_plan_content or {}).get("section_package") or {}
        existing_sections: list[dict] = list(existing_package.get("generated_sections") or [])
        if not existing_sections:
            logger.warning(
                "TestPlanRegenTool: no existing generated_sections; "
                "will rebuild target placeholders from generation_config | "
                "section_ids=%s",
                list(section_ids),
            )

        section_by_id = self._index_by_id(existing_sections)
        # Phase 2.9A.X bug fix: 审计日志暴露 existing_sections 的实际形态与
        # requested_section_ids 的命中情况。修复 REGEN_NOTHING_TARGETABLE
        # 反复触发的根因排查（之前仅有"未匹配"日志，看不到 indexed_keys 全集）。
        logger.warning(
            "TestPlanRegenTool: existing_sections count=%d | indexed_keys=%s | "
            "requested_section_ids=%s",
            len(existing_sections),
            sorted(section_by_id.keys()),
            list(section_ids),
        )
        requested_section_ids = list(section_ids) if bulk_repair else list(
            section_ids[:MAX_REGEN_BATCH]
        )
        targeted = self._resolve_targets(
            requested_section_ids,
            section_by_id=section_by_id,
            gen_config_subset=gen_config_subset,
        )

        if not targeted:
            return self._error(
                "REGEN_NOTHING_TARGETABLE",
                "TestPlanRegenTool: 没有可定位的 section，section_ids 与 config 不匹配",
                recoverable=False,
            )

        # ── 3. Build the focused prompt ───────────────────────────
        issues_by_sid: dict[str, list[dict]] = {}
        for iss in issues:
            sid = iss.get("section_id")
            if sid:
                issues_by_sid.setdefault(sid, []).append(iss)

        payload: dict[str, Any] = {}
        unresolved_targets: list[dict[str, Any]] = []
        regen_warnings: list[str] = []
        target_batches = (
            self._chunked(targeted, MAX_REGEN_BATCH)
            if bulk_repair else [targeted]
        )
        for batch_index, target_batch in enumerate(target_batches, start=1):
            batch_payload, batch_unresolved, batch_warnings = (
                await self._generate_batch_with_split_retry(
                    target_batch,
                    batch_index=batch_index,
                    batch_count=len(target_batches),
                    issues_by_sid=issues_by_sid,
                    context=context,
                    use_mock=use_mock,
                )
            )
            payload.update(batch_payload)
            unresolved_targets.extend(batch_unresolved)
            regen_warnings.extend(batch_warnings)

        # ── 7. Splice back into the section_package ───────────────
        spliced_sections: list[dict] = []
        for tgt in targeted:
            sid = tgt["section_id"]
            field = tgt["field"]
            new_value = payload.get(field)
            if new_value is None:
                logger.warning(
                    "TestPlanRegenTool: field=%s 未出现在 LLM 输出中，跳过", field,
                )
                continue
            # Replace content in-place; preserve all binding metadata.
            tgt["section"]["content"] = new_value
            if tgt.get("missing_section"):
                self._insert_missing_generated_section(
                    existing_sections,
                    tgt["section"],
                    tgt["cfg"],
                    context.template_structure or {},
                )
            spliced_sections.append({
                "section_id": sid,
                "field": field,
                "title": tgt["section"].get("title"),
                "content": new_value,
            })
            # Also update the payload dict so the next review pass sees
            # the same fix on both the section view and the payload view.
            existing_payload = existing_package.get("payload") or {}
            existing_payload[field] = new_value
            existing_package["payload"] = existing_payload

        if not spliced_sections:
            return self._error(
                "REGEN_NO_USABLE_OUTPUT",
                "TestPlanRegenTool: LLM 输出未覆盖任何目标 section",
                recoverable=True,
                warnings=regen_warnings,
                details={
                    "unresolved_section_ids": [
                        str(t.get("section_id"))
                        for t in unresolved_targets
                        if t.get("section_id")
                    ],
                },
            )

        existing_package["generated_sections"] = existing_sections

        # Persist updated package back into ctx
        resolved_fields = {
            str(v)
            for sec in spliced_sections
            for v in (
                sec.get("section_id"),
                sec.get("field"),
                sec.get("title"),
            )
            if v
        }
        if context.test_plan_content is not None:
            context.test_plan_content["section_package"] = existing_package
            # Normalized graph state stores generated_sections as a list.
            # Older legacy envelopes used it as a count; preserve that only
            # when the incoming shape was already count-like.
            if isinstance(context.test_plan_content.get("generated_sections"), list):
                context.test_plan_content["generated_sections"] = list(existing_sections)
            else:
                context.test_plan_content["generated_sections"] = len(existing_sections)
            self._clear_schema_issues_for_fields(
                context.test_plan_content,
                resolved_fields,
            )
            self._clear_generation_recovery_for_fields(
                context.test_plan_content,
                resolved_fields,
            )

        unresolved_section_ids = [
            str(t.get("section_id"))
            for t in unresolved_targets
            if t.get("section_id")
        ]
        summary = f"已重写 {len(spliced_sections)} 个章节"
        if unresolved_section_ids:
            summary += f"，仍有 {len(unresolved_section_ids)} 个章节待下一轮修复"

        return self._success(
            {
                "sections": spliced_sections,
                "splice_into": "test_plan_content.section_package.generated_sections",
                "regenerated_section_ids": [
                    str(sec.get("section_id"))
                    for sec in spliced_sections
                    if sec.get("section_id")
                ],
                "section_ids": [
                    str(sec.get("section_id"))
                    for sec in spliced_sections
                    if sec.get("section_id")
                ],
                "resolved_schema_issue_fields": sorted(resolved_fields),
                "unresolved_section_ids": unresolved_section_ids,
                "partial_success": bool(unresolved_section_ids),
                "issue_count": len(issues),
                "fixed_count": len(spliced_sections),
            },
            summary,
            warnings=regen_warnings,
        )

    # ── helpers ──────────────────────────────────────────────────

    async def _generate_batch_with_split_retry(
        self,
        target_batch: list[dict[str, Any]],
        *,
        batch_index: int,
        batch_count: int,
        issues_by_sid: dict[str, list[dict]],
        context: AgentContext,
        use_mock: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        """Generate a batch; if it fails, split into single-section retries.

        JSON truncation repair can touch most of a document.  A single Context
        Engine selection miss or schema miss in one batch must not discard
        earlier successful batches.  We keep the Context Engine call-site intact
        and reduce only the regen scope, then let the mandatory re-review decide
        the next repair round for any unresolved section.
        """
        try:
            return (
                await self._generate_single_batch_payload(
                    target_batch,
                    issues_by_sid=issues_by_sid,
                    context=context,
                    use_mock=use_mock,
                ),
                [],
                [],
            )
        except (LLMClientError, ResultParseError) as exc:
            logger.warning(
                "TestPlanRegenTool: batch failed; will split retry if possible | "
                "batch=%d/%d | target_count=%d | err=%s",
                batch_index, batch_count, len(target_batch), str(exc)[:240],
            )
            if len(target_batch) <= 1:
                return {}, list(target_batch), [self._target_failure_warning(target_batch, exc)]
        except Exception as exc:  # noqa: BLE001
            logger.exception("TestPlanRegenTool: batch parse/generate unexpected error")
            if len(target_batch) <= 1:
                return {}, list(target_batch), [self._target_failure_warning(target_batch, exc)]

        payload: dict[str, Any] = {}
        unresolved: list[dict[str, Any]] = []
        warnings: list[str] = [
            f"批次 {batch_index}/{batch_count} 重写失败，已自动拆分为单章节重试。"
        ]
        for target in target_batch:
            try:
                single_payload = await self._generate_single_batch_payload(
                    [target],
                    issues_by_sid=issues_by_sid,
                    context=context,
                    use_mock=use_mock,
                )
                payload.update(single_payload)
            except (LLMClientError, ResultParseError) as exc:
                logger.warning(
                    "TestPlanRegenTool: single-section retry failed | section_id=%s | "
                    "field=%s | err=%s",
                    target.get("section_id"), target.get("field"), str(exc)[:240],
                )
                unresolved.append(target)
                warnings.append(self._target_failure_warning([target], exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "TestPlanRegenTool: single-section retry unexpected error | "
                    "section_id=%s | field=%s",
                    target.get("section_id"), target.get("field"),
                )
                unresolved.append(target)
                warnings.append(self._target_failure_warning([target], exc))
        return payload, unresolved, warnings

    async def _generate_single_batch_payload(
        self,
        target_batch: list[dict[str, Any]],
        *,
        issues_by_sid: dict[str, list[dict]],
        context: AgentContext,
        use_mock: bool,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(
            targeted=target_batch,
            issues_by_sid=issues_by_sid,
            full_config=context.template_structure or {},
        )
        raw_json = await self._generate_regen_json(
            prompt,
            target_batch=target_batch,
            context=context,
            use_mock=use_mock,
        )
        try:
            return self._parse_regen_payload(raw_json, target_batch)
        except ResultParseError as exc:
            logger.warning(
                "TestPlanRegenTool: 解析失败 | target_fields=%s | err=%s | raw=%s",
                [t.get("field") for t in target_batch],
                str(exc)[:200],
                raw_json[:200],
            )
            raise

    @staticmethod
    def _target_failure_warning(
        target_batch: list[dict[str, Any]],
        exc: Exception,
    ) -> str:
        section_ids = [
            str(t.get("section_id") or t.get("field") or "?")
            for t in target_batch
        ]
        return (
            "章节 "
            + ", ".join(section_ids)
            + f" 本轮重写未完成：{type(exc).__name__}: {str(exc)[:180]}"
        )

    @staticmethod
    def _index_by_id(sections: list[dict]) -> dict[str, dict]:
        """Build a section_id → section map with **multi-key** indexing.

        每个 section 在 index 字典里挂**所有可用 key**（section_id / id /
        field / title），让上游按任一命名空间传入 section_ids 都能命中。
        这是必要的,因为 section_id（``body_14_level_1``，模板 body 偏移）
        与 ai_field field（``field_body_14_level_1``，P0 修复后用于
        schema_issues / RepairAgent 路由）属于不同命名空间，单 key 索引
        会再次触发 REGEN_NOTHING_TARGETABLE。
        """
        out: dict[str, dict] = {}
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            for key in (
                sec.get("section_id"),
                sec.get("id"),
                sec.get("field"),
                sec.get("title"),
            ):
                if key:
                    out.setdefault(str(key), sec)
        return out

    @classmethod
    def _resolve_targets(
        cls,
        section_ids: list[str],
        *,
        section_by_id: dict[str, dict],
        gen_config_subset: dict,
    ) -> list[dict[str, Any]]:
        targeted: list[dict[str, Any]] = []
        targeted_keys: set[str] = set()
        for sid in section_ids:
            sec = section_by_id.get(sid)
            missing_section = False
            if sec:
                # Translate section_id → generation_config field name
                field = sec.get("field") or sid
                cfg_entry = cls._find_config_entry(gen_config_subset, field, sid)
                if cfg_entry is not None:
                    cls._ensure_section_binding_metadata(sec, cfg_entry)
            else:
                cfg_entry = cls._find_config_entry(gen_config_subset, sid, sid)
                if cfg_entry is None:
                    continue
                missing_section = True
                field = str(cfg_entry.get("field") or sid)
                sec = cls._build_missing_section_placeholder(
                    cfg_entry,
                    str(cfg_entry.get("section_id") or sid),
                    field,
                )
            if cfg_entry is None:
                logger.warning(
                    "TestPlanRegenTool: section %s 在 generation_config_subset 中"
                    "找不到对应配置，跳过 | field=%s", sid, field,
                )
                continue
            dedup_key = str(
                cfg_entry.get("field")
                or cfg_entry.get("section_id")
                or field
                or sid
            )
            if dedup_key in targeted_keys:
                continue
            targeted_keys.add(dedup_key)
            targeted.append({
                "section_id": str(sec.get("section_id") or sid),
                "requested_section_id": sid,
                "section": sec,
                "field": field,
                "cfg": cfg_entry,
                "missing_section": missing_section,
            })
        return targeted

    @staticmethod
    def _chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
        if size <= 0:
            return [items]
        return [items[idx: idx + size] for idx in range(0, len(items), size)]

    async def _generate_regen_json(
        self,
        prompt: str,
        *,
        target_batch: list[dict[str, Any]],
        context: AgentContext,
        use_mock: bool,
    ) -> str:
        """Generate one regen batch while preserving MIG_GENERATE routing."""
        from app.context_engine.feature_flags import require_agent_context_migration

        resolver = getattr(context, "task_flag_resolver", None)
        migration_error = require_agent_context_migration(resolver, "MIG_GENERATE")
        if use_mock:
            llm_client: Any = MockLLMClient()
            return await llm_client.generate(prompt)
        if migration_error is None:
            # CE-04 §四：MIG_GENERATE=true → 只走 ContextInvokerBridge。
            # bridge 缺失视为明确错误（不静默回退 legacy）。
            from app.tools._mig_routing import invoke_via_bridge_or_none
            from app.llm.task_profiles import TEST_PLAN_PROFILE
            from app.agent_runtime.context.test_plan_evidence import (
                build_test_plan_repair_state_ref,
            )

            err, raw = await invoke_via_bridge_or_none(
                context=context,
                call_site="test_plan.repair.regenerate",
                llm_task_profile=TEST_PLAN_PROFILE,
                current_goal=prompt,
                output_contract=self._build_regen_output_contract(target_batch),
                # The focused prompt contains review evidence and target
                # details.  The immutable generation rules themselves must
                # remain a CE system instruction, otherwise a task-state or
                # user-content rendering can demote them to reference text.
                system_prompt=build_test_plan_generation_system_prompt(
                    {
                        "ai_fields": [
                            {
                                **(
                                    target.get("cfg")
                                    if isinstance(target.get("cfg"), dict)
                                    else {}
                                ),
                                "field": str(target.get("field") or ""),
                            }
                            for target in target_batch
                            if isinstance(target, dict)
                            and str(target.get("field") or "").strip()
                        ],
                    }
                ),
                task_state_ref=build_test_plan_repair_state_ref(
                    test_plan_content=getattr(context, "test_plan_content", None),
                    review_result=getattr(context, "review_result", None),
                ),
            )
            if err is not None:
                logger.warning(
                    "TestPlanRegenTool: MIG_GENERATE Invoker 不可用 | code=%s",
                    err,
                )
                raise LLMClientError(f"{err}: 测试方案重写经 Context Invoker 失败")
            if isinstance(raw, str):
                return raw
            return json.dumps(raw, ensure_ascii=False, indent=2)

        raise LLMClientError(
            f"{migration_error}: 测试方案重生成仅支持 Context Engine，已阻止旧 Prompt 回退"
        )

    @staticmethod
    def _build_regen_output_contract(target_batch: list[dict[str, Any]]) -> str:
        """Build a trusted, exact JSON contract for one repair batch.

        The batch is derived by the server from the selected template.  The
        LLM receives it as a Context Engine output contract, not as untrusted
        task evidence or a user-authored repair prompt.
        """
        fields: list[dict[str, Any]] = []
        for target in target_batch:
            if not isinstance(target, dict):
                continue
            field = str(target.get("field") or "").strip()
            cfg = target.get("cfg") if isinstance(target.get("cfg"), dict) else {}
            if field:
                fields.append({**cfg, "field": field})
        return build_test_plan_json_contract(fields)

    @staticmethod
    def _clear_schema_issues_for_fields(
        test_plan_content: dict[str, Any],
        resolved_fields: set[str],
    ) -> None:
        """Drop generator schema issues that were explicitly regenerated."""
        if not resolved_fields:
            return
        schema_issues = test_plan_content.get("schema_issues")
        if not isinstance(schema_issues, list) or not schema_issues:
            return
        remaining: list[dict[str, Any]] = []
        for issue in schema_issues:
            if not isinstance(issue, dict):
                remaining.append(issue)
                continue
            keys = {
                str(v)
                for v in (
                    issue.get("field"),
                    issue.get("section_id"),
                    issue.get("id"),
                    issue.get("title"),
                )
                if v
            }
            if keys and keys & resolved_fields:
                continue
            remaining.append(issue)
        if remaining:
            test_plan_content["schema_issues"] = remaining
        else:
            test_plan_content.pop("schema_issues", None)

    @staticmethod
    def _clear_generation_recovery_for_fields(
        test_plan_content: dict[str, Any],
        resolved_fields: set[str],
    ) -> None:
        if not resolved_fields:
            return
        recovery = test_plan_content.get("generation_recovery")
        if not isinstance(recovery, dict):
            return
        if recovery.get("kind") != "json_truncated":
            return
        fields = [
            str(v)
            for v in recovery.get("missing_fields") or []
            if v and str(v) not in resolved_fields
        ]
        sections = [
            str(v)
            for v in recovery.get("missing_section_ids") or []
            if v and str(v) not in resolved_fields
        ]
        if fields or sections:
            recovery["missing_fields"] = fields
            recovery["missing_section_ids"] = sections
            recovery["missing_count"] = len(fields)
            test_plan_content["generation_recovery"] = recovery
        else:
            test_plan_content.pop("generation_recovery", None)

    @staticmethod
    def _find_config_entry(gen_config_subset: dict, field: str, section_id: str) -> dict | None:
        ai_fields = gen_config_subset.get("ai_fields") or []
        if not isinstance(ai_fields, list):
            return None
        for entry in ai_fields:
            if not isinstance(entry, dict):
                continue
            # 优先 field / section_id 匹配
            if entry.get("field") == field or entry.get("section_id") == section_id:
                return entry
            request_aliases = {
                str(value).strip()
                for value in (entry.get("_request_aliases") or [])
                if str(value or "").strip()
            }
            if field in request_aliases or section_id in request_aliases:
                return entry
            # Phase 2.9A.X bug fix: title 模糊匹配兜底。RepairAgent LLM 构造
            # cfg_subset 时常丢 field / section_id 字段,仅保留 table_schemas,
            # 导致 _find_config_entry 反复 MISS。如果 entry.title 包含 target
            # field/section_id,也按命中处理,允许 TestPlanRegenTool 用真实
            # cfg 渲染 prompt 与 schema 校验。
            entry_title = str(entry.get("title") or "")
            if entry_title and (
                section_id and section_id in entry_title
                or field and field in entry_title
            ):
                return entry
        return None

    @staticmethod
    def _build_missing_section_placeholder(
        cfg_entry: dict,
        section_id: str,
        field: str,
    ) -> dict[str, Any]:
        """Create a generated_section shell for an AI section missing from JSON."""
        section = {
            "section_id": section_id,
            "field": field,
            "title": cfg_entry.get("title") or field or section_id,
            "content": "",
            "placeholder": True,
            "placeholder_reason": "missing_in_generation_result",
        }
        TestPlanRegenTool._ensure_section_binding_metadata(section, cfg_entry)
        return section

    @staticmethod
    def _ensure_section_binding_metadata(section: dict, cfg_entry: dict) -> None:
        """Copy template binding metadata onto a generated_section shell.

        ``json_truncate`` and similar recovery paths may create placeholder
        sections before content exists.  Regen replaces the content later, but
        Word export also needs the original template binding fields to locate
        and safely replace the section.
        """
        if not isinstance(section, dict) or not isinstance(cfg_entry, dict):
            return
        is_placeholder = bool(section.get("placeholder"))
        for key in (
            "section_id",
            "title",
            "clean_title",
            "level",
            "order",
            "path",
            "paragraph_index",
            "body_start_index",
            "body_end_index",
            "table_indexes",
            "table_schemas",
            "source",
            "target_kind",
        ):
            current = section.get(key)
            should_replace_placeholder_identity = (
                is_placeholder
                and key in {"section_id", "title", "clean_title"}
            )
            if (
                current is None or should_replace_placeholder_identity
            ) and key in cfg_entry:
                section[key] = cfg_entry.get(key)
        if not section.get("field") and cfg_entry.get("field"):
            section["field"] = cfg_entry.get("field")

    @staticmethod
    def _issues_for_target(
        target: dict[str, Any],
        issues_by_sid: dict[str, list[dict]],
    ) -> list[dict]:
        """Collect issues across equivalent section_id/field/title namespaces."""
        aliases = {
            str(value)
            for value in (
                target.get("section_id"),
                target.get("requested_section_id"),
                target.get("field"),
                (target.get("section") or {}).get("id"),
                (target.get("section") or {}).get("title"),
                (target.get("cfg") or {}).get("section_id"),
                (target.get("cfg") or {}).get("field"),
                (target.get("cfg") or {}).get("title"),
            )
            if value
        }
        out: list[dict] = []
        seen: set[int] = set()
        for alias in aliases:
            for issue in issues_by_sid.get(alias) or []:
                marker = id(issue)
                if marker in seen:
                    continue
                seen.add(marker)
                out.append(issue)
        return out

    @classmethod
    def _insert_missing_generated_section(
        cls,
        existing_sections: list[dict],
        section: dict,
        cfg_entry: dict,
        template_structure: dict,
    ) -> None:
        """Insert a newly regenerated missing section in template order."""
        section_keys = {
            str(value)
            for value in (
                section.get("section_id"),
                section.get("id"),
                section.get("field"),
                section.get("title"),
            )
            if value
        }
        for idx, existing in enumerate(existing_sections):
            if not isinstance(existing, dict):
                continue
            existing_keys = {
                str(value)
                for value in (
                    existing.get("section_id"),
                    existing.get("id"),
                    existing.get("field"),
                    existing.get("title"),
                )
                if value
            }
            if existing_keys & section_keys:
                existing_sections[idx] = section
                return

        target_order = cls._ai_field_order(cfg_entry, template_structure)
        if target_order is None:
            existing_sections.append(section)
            return

        for idx, existing in enumerate(existing_sections):
            existing_order = cls._generated_section_order(existing, template_structure)
            if existing_order is not None and existing_order > target_order:
                existing_sections.insert(idx, section)
                return
        existing_sections.append(section)

    @staticmethod
    def _ai_fields_from_template(template_structure: dict) -> list[dict]:
        gen_config = (
            template_structure.get("generation_config")
            if isinstance(template_structure, dict)
            else {}
        ) or {}
        ai_fields = gen_config.get("ai_fields") or []
        return [entry for entry in ai_fields if isinstance(entry, dict)]

    @classmethod
    def _ai_field_order(cls, cfg_entry: dict, template_structure: dict) -> int | None:
        wanted = {
            str(value)
            for value in (
                cfg_entry.get("field"),
                cfg_entry.get("section_id"),
                cfg_entry.get("title"),
            )
            if value
        }
        if not wanted:
            return None
        for idx, entry in enumerate(cls._ai_fields_from_template(template_structure)):
            entry_values = {
                str(value)
                for value in (
                    entry.get("field"),
                    entry.get("section_id"),
                    entry.get("title"),
                )
                if value
            }
            if entry_values & wanted:
                return idx
        return None

    @classmethod
    def _generated_section_order(
        cls,
        section: dict,
        template_structure: dict,
    ) -> int | None:
        values = {
            str(value)
            for value in (
                section.get("field"),
                section.get("section_id"),
                section.get("id"),
                section.get("title"),
            )
            if value
        }
        for idx, entry in enumerate(cls._ai_fields_from_template(template_structure)):
            entry_values = {
                str(value)
                for value in (
                    entry.get("field"),
                    entry.get("section_id"),
                    entry.get("title"),
                )
                if value
            }
            if entry_values & values:
                return idx
        return None

    @staticmethod
    def _rebuild_cfg_subset(
        context: AgentContext, section_ids: list[str], inputs: dict,
    ) -> dict:
        """从 context.template_structure 重建 cfg_subset,过滤出 target section_ids。

        Phase 2.9A.X bug fix: RepairAgent 经常传 cfg_subset 缺失 / 残缺。
        这里按 section_ids 从 ai_fields 里挑出匹配 entry(field / section_id /
        title 任一命名空间命中)组装 cfg_subset,确保 TestPlanRegenTool 有完整 cfg。
        """
        ts = (getattr(context, "template_structure", None) or {})
        gc = (ts.get("generation_config") if isinstance(ts, dict) else None) or {}
        ai_fields = gc.get("ai_fields") or []
        if not isinstance(ai_fields, list) or not ai_fields:
            return {"ai_fields": []}

        alias_groups = TestPlanRegenTool._config_alias_groups_from_request(
            section_ids,
            inputs,
        )
        bindings = gc.get("section_bindings") or []
        if isinstance(bindings, list) and bindings:
            TestPlanRegenTool._expand_positional_section_aliases(
                alias_groups,
                bindings,
            )
        wanted = set().union(*alias_groups) if alias_groups else set()
        kept = []
        seen_keys: set[str] = set()
        for entry in ai_fields:
            if not isinstance(entry, dict):
                continue
            matched_aliases = TestPlanRegenTool._matched_entry_aliases(entry, wanted)
            if matched_aliases:
                request_aliases = set(matched_aliases)
                for group in alias_groups:
                    if group & matched_aliases:
                        request_aliases.update(group)
                entry_field = str(entry.get("field") or "")
                entry_sid = str(entry.get("section_id") or "")
                entry_title = str(entry.get("title") or "")
                dedup_key = entry_field or entry_sid or entry_title
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                kept.append({
                    **entry,
                    "_request_aliases": sorted(request_aliases),
                })

        return {**gc, "ai_fields": kept}

    @staticmethod
    def _expand_positional_section_aliases(
        alias_groups: list[set[str]],
        section_bindings: list[dict],
    ) -> None:
        """Resolve legacy frontend ``section_N`` IDs through binding order.

        Older confirmation payloads were built from section dictionaries that
        lacked canonical identities.  The frontend consequently generated
        one-based, depth-first fallback IDs.  ``section_bindings`` uses that
        same order and therefore provides a deterministic compatibility bridge
        for in-flight and previously persisted tasks.
        """
        for aliases in alias_groups:
            expanded: set[str] = set()
            for alias in tuple(aliases):
                match = re.fullmatch(r"section_(\d+)", alias)
                if not match:
                    continue
                index = int(match.group(1)) - 1
                if index < 0 or index >= len(section_bindings):
                    continue
                binding = section_bindings[index]
                if not isinstance(binding, dict):
                    continue
                expanded.update(
                    str(value).strip()
                    for value in (
                        binding.get("section_id"),
                        binding.get("field"),
                        binding.get("suggested_field"),
                        binding.get("title"),
                        binding.get("clean_title"),
                    )
                    if str(value or "").strip()
                )
            aliases.update(expanded)

    @staticmethod
    def _config_aliases_from_request(
        section_ids: list[str],
        inputs: dict,
    ) -> set[str]:
        alias_groups = TestPlanRegenTool._config_alias_groups_from_request(
            section_ids,
            inputs,
        )
        return set().union(*alias_groups) if alias_groups else set()

    @staticmethod
    def _config_alias_groups_from_request(
        section_ids: list[str],
        inputs: dict,
    ) -> list[set[str]]:
        requested_ids: set[str] = {
            str(value).strip()
            for value in section_ids
            if str(value or "").strip()
        }

        def add_value(value: Any, aliases: set[str]) -> None:
            if value is None:
                return
            if isinstance(value, str):
                text = value.strip()
                if text:
                    aliases.add(text)
                    parsed = TestPlanRegenTool._try_parse_json_object(text)
                    if parsed is not None:
                        add_value(parsed, aliases)
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {
                        "field",
                        "field_path",
                        "section_id",
                        "requested_section_id",
                        "source_section_id",
                        "canonical_section_id",
                        "template_section_id",
                        "id",
                        "title",
                        "section_title",
                        "aliases",
                        "missing_fields",
                        "missing_section_ids",
                    }:
                        add_value(item, aliases)
                    elif isinstance(item, (dict, list)):
                        add_value(item, aliases)
                return
            if isinstance(value, list):
                for item in value:
                    add_value(item, aliases)

        groups: list[set[str]] = []
        covered_requested_ids: set[str] = set()
        for issue in inputs.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            # Keep aliases scoped to this one issue.  Seeding every issue with
            # every requested section made each rebuilt config entry claim all
            # targets through ``_request_aliases``.  _find_config_entry then
            # returned the first entry for sibling sections, so only one of a
            # multi-section repair could be regenerated.
            aliases: set[str] = set()
            for name in (
                "section_id",
                "requested_section_id",
                "field",
                "field_path",
                "title",
                "section_title",
                "evidence",
            ):
                add_value(issue.get(name), aliases)
            matched_requested_ids = aliases & requested_ids
            covered_requested_ids.update(matched_requested_ids)
            if not aliases and len(requested_ids) == 1:
                aliases.update(requested_ids)
            if aliases:
                groups.append(aliases)

        # Keep an explicit group for any requested target not represented by a
        # structured issue (for example older recovery callers).
        for section_id in sorted(requested_ids - covered_requested_ids):
            groups.append({section_id})
        if not groups and requested_ids:
            groups.append(set(requested_ids))
        recovery_group: set[str] = set()
        add_value(inputs.get("generation_recovery"), recovery_group)
        if recovery_group:
            groups.append(recovery_group)
        return groups

    @staticmethod
    def _try_parse_json_object(text: str) -> Any:
        if not text or text[0] not in "[{":
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _entry_matches_aliases(entry: dict[str, Any], aliases: set[str]) -> bool:
        return bool(TestPlanRegenTool._matched_entry_aliases(entry, aliases))

    @staticmethod
    def _matched_entry_aliases(entry: dict[str, Any], aliases: set[str]) -> set[str]:
        entry_field = str(entry.get("field") or "").strip()
        entry_sid = str(entry.get("section_id") or "").strip()
        entry_title = str(entry.get("title") or "").strip()
        entry_aliases = {
            value
            for value in (entry_field, entry_sid, entry_title)
            if value
        }
        matched = entry_aliases & aliases
        for alias in aliases:
            if alias and entry_title and alias in entry_title:
                matched.add(alias)
        return matched

    @classmethod
    def _build_prompt(
        cls,
        targeted: list[dict],
        issues_by_sid: dict[str, list[dict]],
        full_config: dict,
    ) -> str:
        """Compose the focused regen prompt.

        Returns a string ready to send to the LLM.  We avoid
        loading ``PromptBuilder.build()`` here because that includes
        the full system-rules preamble — for a focused regen we want a
        tighter scope so the LLM focuses on the fix.
        """
        blocks: list[str] = []
        blocks.append(
            "你正在修复一个已经生成的测试方案中 **少数几个违规章节**。"
            "请严格按照下面的 schema 输出一个 JSON 对象。"
            "除了下面列出的字段之外不要新增任何字段，不要修改其他章节。"
            "保持各章节内部表格的列名与下方「允许表头」一字不差。"
            "正文部分必须是纯中文段落，禁止使用 Markdown（不允许 ```、**、#、行内代码、单引号）。"
        )

        for tgt in targeted:
            sid = tgt["section_id"]
            field = tgt["field"]
            title = tgt["section"].get("title") or field
            cfg = tgt["cfg"]
            field_type = cfg.get("type") or cfg.get("field_type") or "object"

            issues = cls._issues_for_target(tgt, issues_by_sid)
            issue_lines = []
            for iss in issues:
                rule_id = iss.get("rule_id") or iss.get("kind") or "?"
                msg = iss.get("message") or ""
                evidence = coerce_evidence_text(iss.get("evidence")) or ""
                issue_lines.append(
                    f"  - 规则 {rule_id}: {msg}（证据：{evidence[:120]}）"
                )
            issue_block = "\n".join(issue_lines) if issue_lines else "  - (no issues provided)"

            schema_hint = cls._render_field_schema(field, field_type, cfg, tgt["section"])
            blocks.append(
                f"\n## 章节 `{field}`（id={sid}, title={title}）\n"
                f"违规：\n{issue_block}\n"
                f"输出 schema：\n{schema_hint}\n"
                "请重写该字段，**只**输出字段名 → 值的映射，"
                "结构必须严格符合上方 schema。"
            )

        blocks.append(
            "\n## 输出格式\n"
            "只输出一个 JSON 对象，形如：\n"
            "{"
            + ", ".join(f'"{t["field"]}": ...' for t in targeted)
            + "}\n"
            "禁止注释、禁止多余的字段、禁止 Markdown 包裹（如果出现 ``` 也算违规）。"
        )
        return "\n".join(blocks)

    @staticmethod
    def _render_field_schema(field: str, field_type: str, cfg: dict, sec: dict) -> str:
        """Render a per-field schema description for the regen prompt."""
        lines: list[str] = [f"  - field name: {field}"]
        table_shape = table_value_shape_description(cfg)
        lines.append(f"  - value type: {'array' if table_shape else field_type}")
        description = cfg.get("description") or cfg.get("title") or sec.get("title") or ""
        if description:
            lines.append(f"  - description: {description}")

        schemas = cfg.get("table_schemas") or []
        for i, schema in enumerate(schemas):
            if not isinstance(schema, dict):
                continue
            headers = schema.get("headers") or []
            if headers:
                lines.append(
                    f"  - table[{i}] headers (must match exactly): "
                    + ", ".join(f"\"{h}\"" for h in headers)
                )
                rows = schema.get("rows") or []
                if rows:
                    lines.append(f"    example row shape: {json.dumps(rows[0], ensure_ascii=False)}")

        if table_shape:
            lines.append(f"  - {table_shape}")
        elif field_type == "array":
            lines.append("  - value shape: array of objects (one per row)")
        elif field_type == "string":
            lines.append("  - value shape: plain string (paragraphs separated by \\n)")
        else:
            lines.append(
                "  - value shape: JSON object with exactly one body string property"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_regen_payload(
        raw_json: str,
        targeted: list[dict],
    ) -> dict[str, Any]:
        """Extract JSON, then validate that all targeted fields are present.

        We deliberately do NOT run the full ``ResultParser.parse_and_validate_json``
        against the entire ``generation_config`` because the LLM output
        is intentionally a strict subset.  Instead we do a targeted
        shape check per field — the orchestrator's next review will
        re-validate the content with the rule engine.
        """
        try:
            data = extract_json_from_llm_response(raw_json)
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            raise ResultParseError(f"JSON 解析失败：{exc}") from exc

        if not isinstance(data, dict):
            raise ResultParseError("重写结果不是 JSON 对象")

        # Required field presence + coarse type check
        for tgt in targeted:
            field = tgt["field"]
            if field not in data:
                raise ResultParseError(f"缺少必填字段 {field!r}")
            value = data[field]
            cfg = tgt["cfg"]
            expected_type = cfg.get("type") or cfg.get("field_type")
            if expected_type == "array" and not isinstance(value, list):
                raise ResultParseError(f"字段 {field!r} 应为 array，实际 {type(value).__name__}")
            elif expected_type == "string" and not isinstance(value, str):
                raise ResultParseError(f"字段 {field!r} 应为 string，实际 {type(value).__name__}")
            # Use the same contract as the initial generator.  In particular,
            # a prose-only template section cannot be repaired into a table.
            schema_problems = ResultParser._validate_field_schema(field, value, cfg)
            if schema_problems:
                raise ResultParseError("；".join(schema_problems))
        return data
