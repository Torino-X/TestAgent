"""ResultReviewTool — reviews generated test plan quality.

Migrated from legacy `result_parser.py:check_completeness()` and
`test_plan_schema.py:DEFAULT_SECTIONS`, with additional checks per
TestAgent Agent Tool Definition (Section 13).

Capabilities preserved from legacy:
  - check_completeness: verify DEFAULT_SECTIONS are present
  - DEFAULT_SECTIONS: 10 required test plan sections

New capabilities per Agent tool definition:
  - Module coverage check (requirement modules vs generated content)
  - Empty section detection
  - Placeholder detection
  - Too-short content detection
  - Manual fill section identification
  - Three-level review result (passed/warning/failed)

F025 — rule engine:
  When ``ctx.review_standard`` is populated (synthesised by
  :func:`app.common.template_section_service.build_review_standard`),
  the tool walks every rule in the spec, emits a ``severity=block`` or
  ``severity=warn`` issue per finding, and folds them into the existing
  ``level`` decision:

    * any block issue → ``level = "failed"`` and ``passed = False``
    * any warn issue  → ``level = "warning"`` and ``passed = True``
    * no issues       → ``level = "passed"`` and ``passed = True``

  Backward-compat: when ``ctx.review_standard`` is ``None`` the tool
  only runs the hardcoded checks above and behaves exactly as before.

════════════════════════════════════════════════════════════════════════════════
链路位置 (review/regen 路由决定核心):

  节点 review_result_node
    → ResultReviewTool.run(inputs={...}, ctx)
      → 走 review_standard 规则引擎,产出 review_issues(block/warn)
      → 折叠成 level(passed/warning/failed) + passed boolean
      → 同时报告 module_coverage / placeholders / empty_sections 等元数据
      → 写入 context.review_result + state.review_result

调用合约(供开发者速查):
  - 失败错误码:REVIEW_CONTENT_MISSING(无 test_plan_content 触发 Fail-Fast)
                / REVIEW_RESULT_INVALID(review_standard 自检失败);
  - 失败路径分支:
    * level=failed + block_issues → route_after_result_review 决定 regen /
      repair_subgraph / fail_task;
    * level=warning → 走 prepare_export(继续导出);
    * level=passed → 直接 prepare_export。
  - Phase 2.9A.10 Fail-Fast:F023 后的处理:工具执行失败 vs level=warning/failed
    严格区分,只后者触发 regen/repair,工具执行失败直接 fail_task;
  - F025 "template_backfill" 规则在最近修复加入,用于拦截"模板未声明 tables 但
    LLM 生成了 tables"等反向违规。

输出经过 narrative_composer ReviewContextBuilder 包装,生成"审查完成"叙事。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.tools.base import BaseTool

# ── Constants (migrated from legacy test_plan_schema.py) ───────────────

DEFAULT_SECTIONS: list[str] = [
    "项目概述",
    "测试目标",
    "测试范围",
    "测试策略",
    "测试环境",
    "测试资源",
    "测试进度",
    "准入准出标准",
    "风险分析",
    "测试交付物",
]

# Minimum character count for a generated section to be considered non-trivial
_MIN_SECTION_LENGTH = 50

# Patterns indicating placeholder content that needs human input
_PLACEHOLDER_PATTERNS: list[re.Pattern] = [
    re.compile(r"【待补充】"),
    re.compile(r"【待填写】"),
    re.compile(r"【待完善】"),
    re.compile(r"请.{0,10}(补充|填写|完善)"),
    re.compile(r"暂无"),
    re.compile(r"待定"),
    re.compile(r"(?<![\u4e00-\u9fffA-Za-z0-9])略(?![\u4e00-\u9fffA-Za-z0-9])"),
]


class ResultReviewTool(BaseTool):
    name = "ResultReviewTool"
    description = "审查生成的测试方案，检查完整性、覆盖度和常见问题"

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        """Execute review checks on the generated test plan content."""
        test_plan_content = context.test_plan_content
        requirement_analysis = context.requirement_analysis
        template_structure = context.template_structure
        section_confirm_config = context.section_confirm_config

        # Validate required context
        if not test_plan_content:
            return self._error(
                "REVIEW_CONTENT_MISSING",
                "缺少生成的测试方案内容，请先执行 TestPlanGeneratorTool。",
                recoverable=False,
            )

        section_package = test_plan_content.get("section_package", {})
        generated_sections = section_package.get("generated_sections", [])
        keep_sections = section_package.get("keep_sections", [])
        manual_sections = section_package.get("manual_sections", [])
        ai_targets = self._ai_generation_targets(
            section_confirm_config,
            template_structure,
        )
        ai_target_keys = self._target_key_set(ai_targets)
        non_ai_section_keys = self._non_ai_section_keys(
            section_package,
            section_confirm_config,
        )
        target_title_by_key = self._target_title_index(ai_targets)

        issues: list[dict[str, str]] = []
        suggestions: list[str] = []
        empty_sections: list[dict[str, str]] = []
        placeholder_sections: list[dict[str, str]] = []
        legacy_rule_issues: list[dict[str, Any]] = []

        # ── 1. Completeness check (from legacy check_completeness) ────
        missing_ai_sections: list[dict[str, str]] = []
        if ai_targets:
            missing_ai_sections = self._check_ai_required_sections(
                generated_sections,
                ai_targets,
            )
            missing_sections = [s["title"] for s in missing_ai_sections]
        else:
            missing_sections = self._check_completeness(
                test_plan_content,
                template_structure,
            )

        if missing_sections:
            if missing_ai_sections:
                missing_iter = missing_ai_sections
            else:
                missing_iter = [
                    {"section_id": sec, "title": sec}
                    for sec in missing_sections
                ]
            for sec_info in missing_iter:
                sec = sec_info["title"]
                issues.append({
                    "level": "warning",
                    "message": f"缺少必含章节：{sec}",
                })
                legacy_rule_issues.append(self._review_issue(
                    rule_id="missing_required_ai_section",
                    kind="missing_section",
                    section_id=sec_info["section_id"],
                    message=f"用户选择 AI 生成的章节「{sec}」缺失或内容为空",
                    evidence={
                        "section_id": sec_info.get("section_id"),
                        "field": sec_info.get("field"),
                        "title": sec_info.get("title"),
                        "aliases": sec_info.get("aliases", []),
                    },
                    fix_instruction=(
                        f"重新生成章节「{sec}」，必须返回非空正文，并保持模板绑定字段不变。"
                    ),
                    field_path=sec_info.get("field"),
                ))
            suggestions.append("请检查生成的测试方案是否包含所有必要章节")

        # ── 2. Module coverage check ──────────────────────────────────
        covered_module_count = 0
        total_module_count = 0
        if requirement_analysis:
            modules = requirement_analysis.get("modules", [])
            total_module_count = len(modules)
            if total_module_count > 0:
                # Build searchable text from generated sections
                all_content = self._collect_generated_text(generated_sections)
                for module in modules:
                    module_name = module.get("module_name", "") if isinstance(module, dict) else str(module)
                    if module_name and module_name in all_content:
                        covered_module_count += 1

                if covered_module_count < total_module_count:
                    uncovered = total_module_count - covered_module_count
                    issues.append({
                        "level": "warning",
                        "message": f"有 {uncovered} 个业务模块未在生成内容中明确覆盖",
                    })
                    suggestions.append("请确认未覆盖的业务模块是否需要补充")

        # ── 3. Empty sections check ───────────────────────────────────
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            if self._skip_non_ai_review_section(
                section,
                ai_target_keys=ai_target_keys,
                non_ai_section_keys=non_ai_section_keys,
            ):
                continue
            content = section.get("content", "")
            title = self._display_title(section, target_title_by_key)
            if self._is_empty_content(content):
                sid = self._section_key(section) or title
                empty_sections.append({
                    "section_id": sid,
                    "section_title": title,
                    "reason": "生成内容为空",
                })
                issues.append({
                    "level": "warning",
                    "message": f"章节「{title}」生成内容为空",
                })
                legacy_rule_issues.append(self._review_issue(
                    rule_id="empty_ai_section",
                    kind="empty_section",
                    section_id=sid,
                    message=f"用户选择 AI 生成的章节「{title}」生成内容为空",
                    evidence=sid,
                    fix_instruction=(
                        f"重新生成章节「{title}」，输出完整正文，不允许为空。"
                    ),
                ))

        # ── 4. Placeholder check ──────────────────────────────────────
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            if self._skip_non_ai_review_section(
                section,
                ai_target_keys=ai_target_keys,
                non_ai_section_keys=non_ai_section_keys,
            ):
                continue
            content = section.get("content", "")
            title = self._display_title(section, target_title_by_key)
            placeholder_reason = self._detect_placeholder(content)
            if placeholder_reason:
                sid = self._section_key(section) or title
                placeholder_sections.append({
                    "section_id": sid,
                    "section_title": title,
                    "reason": placeholder_reason,
                })
                issues.append({
                    "level": "warning",
                    "message": f"章节「{title}」包含占位符内容：{placeholder_reason}",
                })
                legacy_rule_issues.append(self._review_issue(
                    rule_id="placeholder_in_ai_section",
                    kind="placeholder",
                    section_id=sid,
                    message=f"章节「{title}」包含占位符内容：{placeholder_reason}",
                    evidence=placeholder_reason,
                    fix_instruction=(
                        f"重写章节「{title}」，删除占位符并补充可交付的实际内容。"
                    ),
                ))

        # ── 5. Too-short content check ────────────────────────────────
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            content = section.get("content", "")
            title = section.get("title", section.get("section_id", "未知章节"))
            if isinstance(content, str) and 0 < len(content.strip()) < _MIN_SECTION_LENGTH:
                issues.append({
                    "level": "warning",
                    "message": f"章节「{title}」内容过短（{len(content.strip())} 字符），建议补充",
                })

        # ── 6. Manual fill sections identification ────────────────────
        manual_section_list: list[dict[str, str]] = []
        for section in (manual_sections if isinstance(manual_sections, list) else []):
            if isinstance(section, dict):
                manual_section_list.append({
                    "section_id": section.get("section_id", ""),
                    "section_title": section.get("title", ""),
                    "reason": "用户选择手动填写",
                })

        # ── 7. Determine review level (legacy fallback) ─────────────
        has_empty = len(empty_sections) > 0
        has_missing = len(missing_sections) > 0
        has_issues = len(issues) > 0

        if has_empty and has_missing:
            level = "failed"
            passed = False
        elif has_empty or (has_missing and len(missing_sections) >= 3):
            level = "warning"
            passed = True
        elif has_issues:
            level = "warning"
            passed = True
        else:
            level = "passed"
            passed = True

        # ── 7b. F025 — rule engine (overrides legacy level if any
        #           block-level issues surface) ─────────────────────
        rule_issues: list[dict[str, Any]] = list(legacy_rule_issues)
        if getattr(context, "review_standard", None):
            rule_issues.extend(self._evaluate_rules(
                generated_sections=generated_sections,
                standard=context.review_standard,
            ))

        has_block = any(
            i.get("severity") == "block" for i in rule_issues
        )
        has_warn = any(
            i.get("severity") == "warn" for i in rule_issues
        )
        if has_block:
            level = "failed"
            passed = False
        elif has_warn and level == "passed":
            # Only warn-rule issues → still warning, not passed.
            level = "warning"

        # ── 7c. schema_issues from TestPlanGeneratorTool ────────────────
        # Phase 2.9A.X：TestPlanGeneratorTool 检测到表头/字段 schema
        # 不符时，不抛 ResultParseError，改为把 offending_fields 写入
        # envelope.data.schema_issues，通过 ResultReviewTool 透传给
        # RepairAgent 定点修复。
        #
        # 这里直接读取 schema_issues，把它们转成 rule_issues 格式，
        # 使它们与 F025 规则引擎输出走同一路径（block_issues →
        # route_after_result_review → NODE_REPAIR_SUBGRAPH）。
        # 这样不需要依赖 section_package 里的 tables 字典。
        raw_schema_issues = test_plan_content.get("schema_issues") or []
        schema_issues_from_generator = self._active_schema_issues(
            raw_schema_issues,
            generated_sections=generated_sections,
            template_structure=template_structure,
        )
        if isinstance(raw_schema_issues, list) and (
            len(schema_issues_from_generator) != len(raw_schema_issues)
        ):
            if schema_issues_from_generator:
                test_plan_content["schema_issues"] = schema_issues_from_generator
            else:
                test_plan_content.pop("schema_issues", None)
        if schema_issues_from_generator:
            for si in schema_issues_from_generator:
                if not isinstance(si, dict):
                    continue
                si_kind = si.get("kind", "schema_mismatch")
                si_field = si.get("field", "")
                si_section_id = si.get("section_id") or si_field
                si_message = si.get("message", "")
                rule_issues.append({
                    "rule_id": f"generator_schema_{si_kind}",
                    "kind": f"generator_{si_kind}",
                    "severity": "block",
                    "section_id": si_section_id,
                    "message": si_message or f"字段「{si_field}」存在 schema 问题（来自生成器校验）",
                    "evidence": {
                        "field": si_field,
                        "section_id": si_section_id,
                        "source_error": si.get("source_error"),
                        "recovery_strategy": si.get("recovery_strategy"),
                    },
                    "span": None,
                })
            has_block = any(
                i.get("severity") == "block" for i in rule_issues
            )
            if has_block:
                level = "failed"
                passed = False

        # ── 7d. Backfill contract gate ─────────────────────────────────
        # This gate is independent from review_standard. A review that passes
        # must mean the generated JSON can be backfilled into the Word template
        # without WordExportTool failing because of section/table shape.
        rule_issues.extend(
            self._check_backfill_contract(
                generated_sections=generated_sections,
                template_structure=template_structure,
            )
        )
        rule_issues = self._dedupe_rule_issues(rule_issues)
        has_block = any(
            i.get("severity") == "block" for i in rule_issues
        )
        has_warn = any(
            i.get("severity") == "warn" for i in rule_issues
        )
        if has_block:
            level = "failed"
            passed = False
        elif has_warn and level == "passed":
            level = "warning"

        # ── 8. Build suggestions ──────────────────────────────────────
        if manual_section_list:
            suggestions.append(
                f"有 {len(manual_section_list)} 个章节需要人工补充"
            )
        if not suggestions:
            suggestions.append("测试方案审查通过，可继续导出 Word 文档")

        # ── 9. Build result ───────────────────────────────────────────
        generated_count = len(generated_sections) if isinstance(generated_sections, list) else 0
        kept_count = len(keep_sections) if isinstance(keep_sections, list) else 0
        manual_count = len(manual_sections) if isinstance(manual_sections, list) else 0

        data = {
            "passed": passed,
            "level": level,
            "covered_module_count": covered_module_count,
            "total_module_count": total_module_count,
            "generated_section_count": generated_count,
            "kept_section_count": kept_count,
            "manual_section_count": manual_count,
            "missing_sections": missing_sections,
            "empty_sections": empty_sections,
            "placeholder_sections": placeholder_sections,
            "issues": issues,
            "rule_issues": rule_issues,
            # Phase 2.4 — ADR-2.4-3 + ADR-2.4-12: standardized review_issues
            # list (with issue_id / field_path / expected_rule / repairable /
            # suggested_strategy).  Backward-compatible: callers may still
            # read ``rule_issues`` or ``block_issues``.
            "review_issues": rule_issues,
            "block_issues": [
                {
                    "rule_id": i.get("rule_id"),
                    "section_id": i.get("section_id"),
                    "message": i.get("message"),
                }
                for i in rule_issues
                if i.get("severity") == "block"
            ],
            "suggestions": suggestions,
        }

        context.review_result = data

        summary_parts = []
        if passed:
            summary_parts.append("审查通过")
        else:
            summary_parts.append("审查未通过")
        if total_module_count > 0:
            summary_parts.append(f"业务模块覆盖 {covered_module_count}/{total_module_count}")
        summary_parts.append(f"生成 {generated_count} 个章节")
        if kept_count > 0:
            summary_parts.append(f"保留 {kept_count} 个模板章节")
        if manual_count > 0:
            summary_parts.append(f"手动 {manual_count} 个章节")
        if missing_sections:
            summary_parts.append(f"缺少 {len(missing_sections)} 个必含章节")
        if placeholder_sections:
            summary_parts.append(f"{len(placeholder_sections)} 个章节含占位符")
        if rule_issues:
            block_n = sum(1 for i in rule_issues if i.get("severity") == "block")
            warn_n = sum(1 for i in rule_issues if i.get("severity") == "warn")
            summary_parts.append(f"规则审查: {block_n} 个强制项 / {warn_n} 个警告")

        warnings = [issue["message"] for issue in issues if issue.get("level") == "warning"]
        warnings.extend(
            f"[{i.get('rule_id', 'rule')}] {i.get('message', '')}"
            for i in rule_issues
            if i.get("severity") == "warn"
        )

        return self._success(
            data,
            "，".join(summary_parts),
            warnings=warnings if warnings else None,
        )

    @classmethod
    def _active_schema_issues(
        cls,
        schema_issues: Any,
        *,
        generated_sections: Any,
        template_structure: Any,
    ) -> list[dict[str, Any]]:
        """Return generator schema issues that still fail current content.

        ``schema_issues`` is produced once by TestPlanGeneratorTool.  After
        RepairAgent rewrites a field, it becomes historical evidence; current
        section content is the authority for re-review.
        """
        if not isinstance(schema_issues, list) or not schema_issues:
            return []
        active: list[dict[str, Any]] = []
        for issue in schema_issues:
            if not isinstance(issue, dict):
                continue
            if not cls._schema_issue_resolved(
                issue,
                generated_sections=generated_sections,
                template_structure=template_structure,
            ):
                active.append(issue)
        return active

    @classmethod
    def _schema_issue_resolved(
        cls,
        issue: dict[str, Any],
        *,
        generated_sections: Any,
        template_structure: Any,
    ) -> bool:
        field = str(
            issue.get("field")
            or issue.get("section_id")
            or issue.get("id")
            or ""
        )
        if not field or not isinstance(generated_sections, list):
            return False

        section = cls._find_generated_section(generated_sections, field)
        if not section:
            return False

        cfg = cls._find_generation_field_config(template_structure, field, section)
        if not cfg:
            return False

        expected_type = cfg.get("type") or cfg.get("field_type")
        content = section.get("content")
        if expected_type == "array" and not isinstance(content, list):
            return False
        if expected_type == "string" and not isinstance(content, str):
            return False

        header_sets = []
        for schema in cfg.get("table_schemas") or []:
            if not isinstance(schema, dict):
                continue
            headers = schema.get("headers") or []
            if headers:
                header_sets.append({str(h) for h in headers})
        if not header_sets:
            return True

        tables = cls._extract_table_rows(content)
        if not tables:
            return False
        for table in tables:
            if not table:
                return False
            for row in table:
                if not isinstance(row, dict):
                    return False
                actual = {str(k) for k in row.keys()}
                if not any(actual == expected for expected in header_sets):
                    return False
        return True

    @staticmethod
    def _find_generated_section(
        generated_sections: list[Any],
        field: str,
    ) -> dict[str, Any] | None:
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            keys = {
                str(v)
                for v in (
                    section.get("field"),
                    section.get("section_id"),
                    section.get("id"),
                    section.get("title"),
                )
                if v
            }
            if field in keys:
                return section
        return None

    @staticmethod
    def _find_generation_field_config(
        template_structure: Any,
        field: str,
        section: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(template_structure, dict):
            return None
        generation_config = template_structure.get("generation_config") or {}
        ai_fields = generation_config.get("ai_fields") or []
        if not isinstance(ai_fields, list):
            return None
        section_keys = {
            str(v)
            for v in (
                field,
                section.get("field"),
                section.get("section_id"),
                section.get("id"),
                section.get("title"),
            )
            if v
        }
        for cfg in ai_fields:
            if not isinstance(cfg, dict):
                continue
            cfg_keys = {
                str(v)
                for v in (
                    cfg.get("field"),
                    cfg.get("section_id"),
                    cfg.get("id"),
                    cfg.get("title"),
                )
                if v
            }
            if cfg_keys & section_keys:
                return cfg
        return None

    # ── Completeness check (migrated from legacy) ─────────────────────

    @staticmethod
    def _review_issue(
        *,
        rule_id: str,
        kind: str,
        section_id: str,
        message: str,
        evidence: Any = None,
        fix_instruction: str | None = None,
        field_path: str | None = None,
    ) -> dict[str, Any]:
        issue = {
            "rule_id": rule_id,
            "kind": kind,
            "severity": "block",
            "section_id": str(section_id or ""),
            "message": message,
            "evidence": evidence,
            "span": None,
            "repairable": True,
            "suggested_strategy": "regenerate_section",
        }
        if field_path:
            issue["field_path"] = field_path
        if fix_instruction:
            issue["fix_instruction"] = fix_instruction
        return issue

    @classmethod
    def _ai_generation_targets(
        cls,
        section_confirm_config: Any,
        template_structure: Any,
    ) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        cfg_sections = (
            section_confirm_config.get("sections")
            if isinstance(section_confirm_config, dict)
            else None
        )
        if isinstance(cfg_sections, list):
            for section in cfg_sections:
                if not isinstance(section, dict):
                    continue
                action = str(
                    section.get("suggested_action")
                    or section.get("action")
                    or ""
                ).lower()
                if action != "ai_generate":
                    continue
                target = cls._target_from_section(section)
                if target:
                    targets.append(target)
            if targets:
                return cls._dedupe_targets(
                    cls._canonicalize_ai_targets(targets, template_structure)
                )

        gen_config = (
            template_structure.get("generation_config")
            if isinstance(template_structure, dict)
            else None
        ) or {}
        ai_fields = gen_config.get("ai_fields") if isinstance(gen_config, dict) else None
        if isinstance(ai_fields, list):
            for entry in ai_fields:
                if isinstance(entry, dict):
                    target = cls._target_from_section(entry)
                    if target:
                        targets.append(target)
        return cls._dedupe_targets(targets)

    @staticmethod
    def _target_from_section(section: dict[str, Any]) -> dict[str, Any] | None:
        field = str(section.get("field") or "").strip()
        section_id = str(section.get("section_id") or section.get("id") or "").strip()
        title = str(section.get("title") or section.get("clean_title") or "").strip()
        if not (field or section_id or title):
            return None
        return {
            "field": field,
            "section_id": section_id or field or title,
            "title": title or section_id or field,
        }

    @staticmethod
    def _dedupe_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for target in targets:
            key = target.get("section_id") or target.get("field") or target.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(target)
        return out

    @staticmethod
    def _ai_fields_from_template(template_structure: Any) -> list[dict[str, Any]]:
        if not isinstance(template_structure, dict):
            return []
        generation_config = template_structure.get("generation_config") or {}
        if not isinstance(generation_config, dict):
            return []
        ai_fields = generation_config.get("ai_fields") or []
        if not isinstance(ai_fields, list):
            return []
        return [entry for entry in ai_fields if isinstance(entry, dict)]

    @classmethod
    def _canonicalize_ai_targets(
        cls,
        targets: list[dict[str, Any]],
        template_structure: Any,
    ) -> list[dict[str, Any]]:
        ai_fields = cls._ai_fields_from_template(template_structure)
        if not ai_fields:
            return targets

        out: list[dict[str, Any]] = []
        used_indexes: set[int] = set()
        for idx, target in enumerate(targets):
            cfg_index = cls._find_ai_field_index_for_target(
                target,
                ai_fields,
                preferred_index=idx,
                used_indexes=used_indexes,
            )
            if cfg_index is None:
                out.append(target)
                continue
            used_indexes.add(cfg_index)
            cfg = ai_fields[cfg_index]
            aliases = cls._target_key_set([target]) | cls._target_key_set([cfg])
            canonical = {
                **target,
                "field": str(cfg.get("field") or target.get("field") or "").strip(),
                "section_id": str(
                    cfg.get("section_id")
                    or target.get("section_id")
                    or cfg.get("field")
                    or target.get("field")
                    or ""
                ).strip(),
                "title": str(cfg.get("title") or target.get("title") or "").strip(),
                "aliases": sorted(alias for alias in aliases if alias),
            }
            out.append(canonical)
        return out

    @classmethod
    def _find_ai_field_index_for_target(
        cls,
        target: dict[str, Any],
        ai_fields: list[dict[str, Any]],
        *,
        preferred_index: int,
        used_indexes: set[int],
    ) -> int | None:
        target_keys = cls._target_key_set([target])
        target_titles = {
            cls._normalize_match_text(str(value))
            for value in (
                target.get("title"),
                target.get("clean_title"),
                target.get("section_title"),
            )
            if value
        }

        for idx, entry in enumerate(ai_fields):
            if idx in used_indexes or not isinstance(entry, dict):
                continue
            entry_keys = cls._target_key_set([entry])
            if target_keys & entry_keys:
                return idx
            entry_title = cls._normalize_match_text(str(entry.get("title") or ""))
            if entry_title and entry_title in target_titles:
                return idx

        section_id = str(target.get("section_id") or "").strip()
        match = re.fullmatch(r"section_(\d+)", section_id)
        if match:
            num = int(match.group(1))
            candidate_indexes = [num - 1]
            if num == 0:
                candidate_indexes = [0]
            for idx in candidate_indexes:
                if 0 <= idx < len(ai_fields) and idx not in used_indexes:
                    return idx

        if preferred_index < len(ai_fields) and preferred_index not in used_indexes:
            return preferred_index
        return None

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        return re.sub(r"[\s　]+", "", value.strip()).lower()

    @classmethod
    def _target_key_set(cls, targets: list[dict[str, Any]]) -> set[str]:
        keys: set[str] = set()
        for target in targets:
            for name in ("field", "section_id", "title"):
                value = str(target.get(name) or "").strip()
                if value:
                    keys.add(value)
            aliases = target.get("aliases")
            if isinstance(aliases, list):
                for alias in aliases:
                    value = str(alias or "").strip()
                    if value:
                        keys.add(value)
        return keys

    @classmethod
    def _target_title_index(cls, targets: list[dict[str, Any]]) -> dict[str, str]:
        index: dict[str, str] = {}
        for target in targets:
            title = str(target.get("title") or "").strip()
            if not title:
                continue
            for key in cls._target_key_set([target]):
                index[key] = title
        return index

    @classmethod
    def _non_ai_section_keys(
        cls,
        section_package: Any,
        section_confirm_config: Any,
    ) -> set[str]:
        keys: set[str] = set()

        def add_section(section: Any) -> None:
            if isinstance(section, str):
                if section.strip():
                    keys.add(section.strip())
                return
            if not isinstance(section, dict):
                return
            for name in ("field", "section_id", "id", "title", "clean_title"):
                value = str(section.get(name) or "").strip()
                if value:
                    keys.add(value)

        if isinstance(section_package, dict):
            for group in ("keep_sections", "manual_sections"):
                values = section_package.get(group)
                if isinstance(values, list):
                    for section in values:
                        add_section(section)

        cfg_sections = (
            section_confirm_config.get("sections")
            if isinstance(section_confirm_config, dict)
            else None
        )
        if isinstance(cfg_sections, list):
            for section in cfg_sections:
                if not isinstance(section, dict):
                    continue
                action = str(
                    section.get("suggested_action")
                    or section.get("action")
                    or ""
                ).lower()
                if action in {"keep_template", "manual_fill", "skip"}:
                    add_section(section)

        return keys

    @staticmethod
    def _section_keys(section: dict[str, Any]) -> set[str]:
        keys: set[str] = set()
        for name in ("field", "section_id", "id", "title", "clean_title"):
            value = str(section.get(name) or "").strip()
            if value:
                keys.add(value)
        return keys

    @classmethod
    def _section_key(cls, section: dict[str, Any]) -> str:
        for name in ("section_id", "field", "id", "title", "clean_title"):
            value = str(section.get(name) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _skip_non_ai_review_section(
        cls,
        section: dict[str, Any],
        *,
        ai_target_keys: set[str],
        non_ai_section_keys: set[str],
    ) -> bool:
        keys = cls._section_keys(section)
        if keys & non_ai_section_keys:
            return True
        if ai_target_keys and not (keys & ai_target_keys):
            return True
        return False

    @classmethod
    def _display_title(
        cls,
        section: dict[str, Any],
        target_title_by_key: dict[str, str],
    ) -> str:
        for key in cls._section_keys(section):
            title = target_title_by_key.get(key)
            if title:
                return title
        return str(
            section.get("title")
            or section.get("clean_title")
            or section.get("section_id")
            or section.get("field")
            or "未知章节"
        )

    @classmethod
    def _check_ai_required_sections(
        cls,
        generated_sections: Any,
        ai_targets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(generated_sections, list):
            generated_sections = []

        section_by_key: dict[str, dict[str, Any]] = {}
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            for key in cls._section_keys(section):
                section_by_key[key] = section

        missing: list[dict[str, Any]] = []
        for target in ai_targets:
            target_keys = cls._target_key_set([target])
            section = next(
                (section_by_key[key] for key in target_keys if key in section_by_key),
                None,
            )
            if section is not None and not cls._is_empty_content(section.get("content")):
                continue
            section_id = target.get("section_id") or target.get("field") or target.get("title")
            title = target.get("title") or section_id
            missing.append({
                "section_id": str(section_id or title),
                "field": str(target.get("field") or ""),
                "title": str(title or section_id),
                "aliases": sorted(cls._target_key_set([target])),
            })
        return missing

    def _check_completeness(
        self,
        test_plan_content: dict[str, Any],
        template_structure: dict[str, Any] | None,
    ) -> list[str]:
        """Check if generated content covers all DEFAULT_SECTIONS.

        Migrated from legacy `ResultParser.check_completeness()`.
        Uses template_structure sections if available, falls back to
        DEFAULT_SECTIONS.
        """
        # Build the set of required section titles
        required_titles = set(DEFAULT_SECTIONS)

        # If template_structure is available, also include its section titles
        if template_structure:
            sections = template_structure.get("sections", [])
            for section in sections:
                if isinstance(section, dict):
                    title = section.get("title", "")
                    if title and section.get("level") == 1:
                        required_titles.add(title)

        # Build searchable text from test_plan_content
        searchable = self._build_searchable_text(test_plan_content)

        # Check which required sections are missing
        missing = []
        for section_title in required_titles:
            if section_title not in searchable:
                missing.append(section_title)

        return missing

    def _build_searchable_text(self, test_plan_content: dict[str, Any]) -> str:
        """Build a searchable text string from test_plan_content."""
        parts = []

        # From section_package generated_sections
        section_package = test_plan_content.get("section_package", {})
        for section in section_package.get("generated_sections", []):
            if isinstance(section, dict):
                title = section.get("title", "")
                content = section.get("content", "")
                if title:
                    parts.append(title)
                if isinstance(content, str):
                    parts.append(content.replace(" ", ""))

        # From section_package keep_sections (titles)
        # BUG FIX 2026-08-18 (keep_sections):keep_sections 可能是 list[str]
        # (纯标题,template_prompt_preview_service.py:62 产出)或 list[dict],
        # 旧实现用 isinstance(section, dict) 过滤会把 list[str] 全漏掉,
        # 导致 mode=keep 的章节被 _check_completeness 误判为缺失。
        for section in section_package.get("keep_sections", []):
            if isinstance(section, dict):
                title = section.get("title", "")
            elif isinstance(section, str):
                title = section
            else:
                continue
            if title:
                parts.append(title)

        return "".join(parts)

    # ── Content analysis helpers ───────────────────────────────────────

    def _collect_generated_text(self, generated_sections: list) -> str:
        """Collect all text from generated sections for module coverage check."""
        parts = []
        for section in generated_sections:
            if isinstance(section, dict):
                content = section.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                # Also check table content
                tables = section.get("tables", [])
                if isinstance(tables, list):
                    for table in tables:
                        if isinstance(table, list):
                            for row in table:
                                if isinstance(row, dict):
                                    parts.extend(str(v) for v in row.values())
        return "".join(parts)

    @staticmethod
    def _is_empty_content(content: Any) -> bool:
        """Check if content is empty or contains only whitespace."""
        if content is None:
            return True
        if isinstance(content, str):
            return len(content.strip()) == 0
        if isinstance(content, list):
            return len(content) == 0
        return False

    @staticmethod
    def _detect_placeholder(content: Any) -> str | None:
        """Detect placeholder patterns in content.

        Returns the matched pattern description, or None if no placeholder found.
        """
        if not isinstance(content, str) or not content.strip():
            return None

        for pattern in _PLACEHOLDER_PATTERNS:
            match = pattern.search(content)
            if match:
                return f"检测到占位符「{match.group()}」"

        return None

    # ── F025 — rule engine ──────────────────────────────────────────

    def _evaluate_rules(
        self,
        generated_sections: list[dict],
        standard: dict,
    ) -> list[dict[str, Any]]:
        """Walk every rule in ``standard`` and emit one issue per finding.

        Each returned issue has the shape:

            {
                "rule_id":    str,
                "kind":       str,            # mirrors rule.kind
                "severity":   "block" | "warn",
                "section_id": str | None,    # the section that triggered it
                "message":    str,
                "evidence":   str | None,    # first 200 chars of offending snippet
                "span":       tuple | None,  # (start, end) char offsets in section body
            }

        ``span`` is best-effort: regex rules fill it in, structural
        rules (table_header_whitelist, section_format) may leave it None.

        The orchestrator's review-regen loop reads only the
        ``section_id`` + ``severity`` + ``message`` fields; everything
        else is for the UI / audit log.
        """
        if not isinstance(standard, dict):
            return []
        rules = standard.get("rules", [])
        if not isinstance(rules, list):
            return []

        issues: list[dict[str, Any]] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            kind = rule.get("kind")
            handler = {
                "forbidden_pattern": self._check_forbidden_pattern,
                "json_key_rename": self._check_json_key_rename,
                "table_header_whitelist": self._check_table_header_whitelist,
                "section_format": self._check_section_format,
                "max_chars": self._check_max_chars,
                "missing_section": self._check_missing_section,
                # BUG FIX 2026-08-18 (template_backfill): 检测"模板未声明
                # tables 但 LLM 生成了 tables"等反向违规。rule body 由
                # build_review_standard 在 mode=="ai" + table_indexes==[] +
                # leaf section 时合成。handler 无法访问 template_structure，
                # 所有模板元数据通过 rule["section_id"] / rule["expected_no_tables"]
                # 传入。
                "template_backfill": self._check_template_backfill,
            }.get(kind)
            if handler is None:
                continue
            try:
                issues.extend(handler(generated_sections, rule))
            except Exception as exc:  # never let one rule break the whole pass
                logger.warning(
                    "ResultReviewTool: rule %s raised | err=%s",
                    rule.get("id"), exc,
                )
        # Phase 2.9A.X: 把 rule 的 fix_instruction 注入到每条 issue
        # 让 _standardize_issue 透传到 RepairAgent。
        rule_fix_instruction_by_id: dict[str, str] = {}
        for rule in rules:
            if isinstance(rule, dict) and rule.get("fix_instruction"):
                rule_fix_instruction_by_id[rule.get("id", "")] = rule["fix_instruction"]
        if rule_fix_instruction_by_id:
            for issue in issues:
                rid = issue.get("rule_id", "")
                if rid in rule_fix_instruction_by_id and not issue.get("fix_instruction"):
                    issue["fix_instruction"] = rule_fix_instruction_by_id[rid]
        # Phase 2.4: standardize each issue with repair-friendly fields
        return [self._standardize_issue(i, idx) for idx, i in enumerate(issues)]

    @staticmethod
    def _standardize_issue(issue: dict[str, Any], idx: int) -> dict[str, Any]:
        """Add repair-friendly fields (Phase 2.4 ADR-2.4-3).

        Each issue gains:
          * ``issue_id``           — stable across runs (rule_id + section_id + idx)
          * ``field_path``         — JSON-path-like location string
          * ``expected_rule``      — what rule wants (e.g. "no_forbidden_patterns")
          * ``repairable``         — bool; False for max_chars (token economy)
          * ``suggested_strategy`` — one of the REPAIR_STRATEGIES

        Backward-compat: original rule fields (``rule_id``, ``kind``, ``severity``,
        ``section_id``, ``message``, ``evidence``, ``span``) are preserved.
        """
        rule_id = issue.get("rule_id") or "rule"
        section_id = issue.get("section_id") or ""
        kind = issue.get("kind") or "unknown"
        issue_id = f"{rule_id}:{section_id or 'global'}:{idx}"

        # field_path: best-effort JSON path
        if section_id and kind == "table_header_whitelist":
            field_path = f"section.{section_id}.tables[0].header"
        elif section_id:
            field_path = f"section.{section_id}.content"
        else:
            field_path = "test_plan_content.global"

        # expected_rule: derive from kind
        expected_rule_map = {
            "forbidden_pattern": "no_forbidden_patterns",
            "json_key_rename": "use_canonical_key",
            "table_header_whitelist": "table_header_matches_template",
            "section_format": "section_matches_required_format",
            "max_chars": "section_within_max_length",
            # BUG FIX 2026-08-18 (template_backfill): 章节内容应符合模板
            # 结构约束（mode/table_indexes 等 schema 元数据）。
            "template_backfill": "section_matches_template",
            "backfill_contract": "json_backfillable_to_word_template",
        }
        expected_rule = expected_rule_map.get(kind, f"rule:{kind}")

        # repairable: max_chars is read-only (token economy; cannot regenerate without LLM)
        repairable = kind != "max_chars"

        # suggested_strategy: phase 2.4 only one strategy; Phase 2.5+ will add table/section-level
        strategy_map = {
            "forbidden_pattern": "regenerate_section",
            "json_key_rename": "regenerate_section",
            "table_header_whitelist": "regenerate_section",
            "section_format": "regenerate_section",
            "max_chars": "regenerate_section",
            # BUG FIX 2026-08-18 (template_backfill): 模板反向违规由
            # RepairAgent 调 TestPlanRegenTool 重新生成该章节，与其他
            # 内容质量规则走同一策略。
            "template_backfill": "regenerate_section",
            "backfill_contract": "regenerate_section",
        }
        suggested_strategy = strategy_map.get(kind, "regenerate_section")

        standardized = dict(issue)
        standardized.update(
            {
                "issue_id": issue_id,
                "field_path": field_path,
                "expected_rule": expected_rule,
                "repairable": repairable,
                "suggested_strategy": suggested_strategy,
            }
        )
        # Phase 2.9A.X: fix_instruction 透传给 RepairAgent
        # 调用方（review_result_node 或 issue_parser）需要把 rule 携带
        # 的 fix_instruction 一路透传到 TestPlanRegenTool._build_prompt。
        # 这里先把 issue 自带的 fix_instruction 透出来（如果 rule handler
        # 显式塞入），没有就保持 None，由 issue_parser 后续从 rule 补。
        if issue.get("fix_instruction"):
            standardized["fix_instruction"] = issue["fix_instruction"]
        return standardized

    @staticmethod
    def _section_id_for_rule(rule: dict, section: dict) -> bool:
        """Return True if ``rule`` applies to ``section`` per
        ``applies_to_sections`` (defaults to ["*"] = all sections)."""
        applies = rule.get("applies_to_sections") or ["*"]
        if "*" in applies:
            return True
        sid = section.get("section_id") or section.get("id") or ""
        stitle = section.get("title") or ""
        return sid in applies or stitle in applies

    @staticmethod
    def _check_forbidden_pattern(
        generated_sections: list[dict],
        rule: dict,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        patterns = rule.get("patterns") or []
        if not patterns:
            return issues
        compiled: list[tuple[str, re.Pattern]] = []
        for p in patterns:
            try:
                compiled.append((p, re.compile(p, re.MULTILINE)))
            except re.error:
                continue
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            if not ResultReviewTool._section_id_for_rule(rule, section):
                continue
            body = section.get("content", "")
            if not isinstance(body, str):
                continue
            for src, pat in compiled:
                m = pat.search(body)
                if m:
                    issues.append({
                        "rule_id": rule.get("id", "forbidden_pattern"),
                        "kind": "forbidden_pattern",
                        "severity": rule.get("severity", "block"),
                        "section_id": section.get("section_id") or section.get("id"),
                        "message": f"内容包含违规模式「{src}」",
                        "evidence": m.group(0)[:200],
                        "span": m.span(),
                    })
                    break  # one finding per section per rule
        return issues

    @staticmethod
    def _check_json_key_rename(
        generated_sections: list[dict],
        rule: dict,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        forbidden = rule.get("forbidden_renames") or {}
        if not isinstance(forbidden, dict) or not forbidden:
            return issues
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            if not ResultReviewTool._section_id_for_rule(rule, section):
                continue
            body = section.get("content", "")
            if not isinstance(body, str):
                continue
            for original, alts in forbidden.items():
                if not isinstance(alts, list):
                    continue
                for alt in alts:
                    if not isinstance(alt, str) or not alt:
                        continue
                    if alt in body:
                        m = re.search(re.escape(alt), body)
                        issues.append({
                            "rule_id": rule.get("id", "json_key_rename"),
                            "kind": "json_key_rename",
                            "severity": rule.get("severity", "block"),
                            "section_id": section.get("section_id") or section.get("id"),
                            "message": (
                                f"键名「{alt}」应为「{original}」"
                            ),
                            "evidence": alt,
                            "span": m.span() if m else None,
                        })
                        break  # one finding per (section, original)
        return issues

    @staticmethod
    def _check_table_header_whitelist(
        generated_sections: list[dict],
        rule: dict,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        whitelist = set(rule.get("whitelist") or [])
        target_id = rule.get("section_id")
        if not whitelist or not target_id:
            return issues
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            sid = section.get("section_id") or section.get("id")
            if sid != target_id:
                continue
            # BUG FIX 2026-08-19：原实现读 ``section["tables"]``，但
            # build_section_package 写入的是 ``content``。统一走
            # ``_extract_table_rows`` 归一化为 ``list[list[dict]]``，
            # 既支持单表 ``list[dict]`` 也支持多表 ``list[list[dict]]``。
            tables = ResultReviewTool._extract_table_rows(section.get("content"))
            # 向后兼容：旧数据若 ``section["tables"]`` 是 list of list of dict
            # 也纳入校验；新数据只走 content 分支。
            legacy_tables = section.get("tables")
            if isinstance(legacy_tables, list) and legacy_tables:
                if isinstance(legacy_tables[0], list):
                    tables = tables + [
                        [r for r in sub if isinstance(r, dict)]
                        for sub in legacy_tables
                        if isinstance(sub, list)
                    ]
            for table in tables:
                if not table:
                    continue
                header_row = table[0]
                if not isinstance(header_row, dict):
                    continue
                actual = set(header_row.keys())
                extra = actual - whitelist
                missing = whitelist - actual
                if extra or missing:
                    msg_parts = []
                    if extra:
                        msg_parts.append(f"多余键 {sorted(extra)}")
                    if missing:
                        msg_parts.append(f"缺少键 {sorted(missing)}")
                    issues.append({
                        "rule_id": rule.get("id", "table_header_whitelist"),
                        "kind": "table_header_whitelist",
                        "severity": rule.get("severity", "block"),
                        "section_id": sid,
                        "message": "表格表头不符合模板: " + "，".join(msg_parts),
                        "evidence": ", ".join(actual),
                        "span": None,
                    })
        return issues

    @staticmethod
    def _check_section_format(
        generated_sections: list[dict],
        rule: dict,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        target_id = rule.get("section_id")
        fmt = rule.get("format")
        if not target_id or fmt != "bullet_list":
            return issues
        min_items = int(rule.get("min_items", 1))
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            sid = section.get("section_id") or section.get("id")
            if sid != target_id:
                continue
            body = section.get("content", "")
            if not isinstance(body, str):
                continue
            # Heuristic: count lines that start with a bullet marker
            bullet_re = re.compile(r"^\s*(?:[-*•·]|[•·]|\d+[.)、])\s+\S", re.MULTILINE)
            bullets = bullet_re.findall(body)
            if len(bullets) < min_items:
                issues.append({
                    "rule_id": rule.get("id", "section_format"),
                    "kind": "section_format",
                    "severity": rule.get("severity", "block"),
                    "section_id": sid,
                    "message": (
                        f"章节应为 bullet_list 格式且至少 {min_items} 条，"
                        f"实际 {len(bullets)} 条"
                    ),
                    "evidence": body[:120],
                    "span": None,
                })
        return issues

    @staticmethod
    def _check_max_chars(
        generated_sections: list[dict],
        rule: dict,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        max_chars = int(rule.get("max_chars", 4000))
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            body = section.get("content", "")
            if not isinstance(body, str):
                continue
            n = len(body)
            if n > max_chars:
                issues.append({
                    "rule_id": rule.get("id", "max_chars"),
                    "kind": "max_chars",
                    "severity": rule.get("severity", "warn"),
                    "section_id": section.get("section_id") or section.get("id"),
                    "message": f"章节长度 {n} 超过 {max_chars} 字符",
                    "evidence": f"length={n}",
                    "span": None,
                })
        return issues

    @staticmethod
    def _check_missing_section(
        generated_sections: list[dict],
        rule: dict,
    ) -> list[dict[str, Any]]:
        """检测生成结果中缺失的章节。

        Phase 2.9A.X：宽容解析 / 整体 JSON 截断场景下，result_parser
        已经把缺失字段标成 ``missing_field`` issue。但 ``generated_sections``
        这一层的 section 缺失是 ResultReviewTool 唯一能检测的——它同时
        持有 ``generated_sections`` 列表和 ``template_structure.sections``
        期望集合（通过 rule body 的 ``expected_section_ids`` 传入）。

        每个缺失的 section 发一条 block issue，携带 ``fix_instruction``
        供 RepairAgent 定点补生成。
        """
        issues: list[dict[str, Any]] = []
        expected = rule.get("expected_section_ids") or []
        if not expected:
            return issues

        actual = set()
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            sid = section.get("section_id") or section.get("id") or section.get("field")
            if sid:
                actual.add(str(sid))

        missing = [str(s) for s in expected if str(s) not in actual]
        if not missing:
            return issues

        issues.append({
            "rule_id": rule.get("id", "missing_section"),
            "kind": "missing_section",
            "severity": rule.get("severity", "block"),
            "section_id": missing[0],  # 第一个触发 section_id，message 列出全部
            "message": (
                f"以下章节在生成结果中缺失：{'、'.join(f'「{m}」' for m in missing)}"
            ),
            "evidence": ",".join(missing),
            "span": None,
            # missing_sections 列表整体附在 fix_instruction 里（LLM 取走用）
            "missing_section_ids": missing,
        })
        return issues

    @classmethod
    def _check_backfill_contract(
        cls,
        *,
        generated_sections: Any,
        template_structure: Any = None,
    ) -> list[dict[str, Any]]:
        """Validate JSON section payloads against Word backfill constraints.

        This mirrors the WordExporter hard-fail contract before export:
        JSON table blocks must not exceed or contradict the template's declared
        table placeholders. Issues are block-level because they must enter the
        RepairAgent loop instead of surfacing as WordExportTool failures.
        """
        if not isinstance(generated_sections, list):
            return []

        issues: list[dict[str, Any]] = []
        for idx, section in enumerate(generated_sections):
            if not isinstance(section, dict):
                issues.append(cls._backfill_issue(
                    section_id=f"section_{idx}",
                    message="生成结果章节不是 JSON 对象，无法回填到 Word 模板",
                    evidence={"actual_type": type(section).__name__},
                ))
                continue

            section_id = str(
                section.get("section_id")
                or section.get("id")
                or section.get("field")
                or f"section_{idx}"
            )
            cfg = cls._find_generation_field_config(
                template_structure, section_id, section
            )
            table_blocks = cls._extract_export_table_blocks(section)
            declared_count = cls._declared_table_placeholder_count(section, cfg)

            if declared_count == 0:
                if table_blocks:
                    issues.append(cls._backfill_issue(
                        section_id=section_id,
                        message=(
                            "模板未声明该章节的表格占位，但 JSON 返回了 "
                            f"{len(table_blocks)} 张表格数据"
                        ),
                        evidence={
                            "declared_table_count": declared_count,
                            "actual_table_count": len(table_blocks),
                        },
                    ))
                continue

            if not table_blocks:
                issues.append(cls._backfill_issue(
                    section_id=section_id,
                    message=(
                        f"模板声明了 {declared_count} 个表格占位，但 JSON 未返回表格数据"
                    ),
                    evidence={
                        "declared_table_count": declared_count,
                        "actual_table_count": 0,
                    },
                ))
                continue

            if len(table_blocks) != declared_count:
                issues.append(cls._backfill_issue(
                    section_id=section_id,
                    message=(
                        f"模板声明了 {declared_count} 个表格占位，但 JSON 返回了 "
                        f"{len(table_blocks)} 张表格数据"
                    ),
                    evidence={
                        "declared_table_count": declared_count,
                        "actual_table_count": len(table_blocks),
                    },
                ))
                continue

            header_issues = cls._check_declared_table_headers(
                section=section,
                cfg=cfg,
                table_blocks=table_blocks,
                section_id=section_id,
            )
            issues.extend(header_issues)

        return issues

    @staticmethod
    def _backfill_issue(
        *,
        section_id: str,
        message: str,
        evidence: Any,
    ) -> dict[str, Any]:
        return {
            "rule_id": "backfill_contract",
            "kind": "backfill_contract",
            "severity": "block",
            "section_id": section_id,
            "message": message,
            "evidence": evidence,
            "span": None,
        }

    @staticmethod
    def _declared_table_placeholder_count(
        section: dict[str, Any],
        cfg: dict[str, Any] | None = None,
    ) -> int:
        table_schemas = section.get("table_schemas")
        table_indexes = section.get("table_indexes")
        if cfg:
            table_schemas = table_schemas or cfg.get("table_schemas")
            table_indexes = table_indexes or cfg.get("table_indexes")
        schema_count = len(table_schemas) if isinstance(table_schemas, list) else 0
        index_count = len(table_indexes) if isinstance(table_indexes, list) else 0
        return max(schema_count, index_count)

    @classmethod
    def _check_declared_table_headers(
        cls,
        *,
        section: dict[str, Any],
        cfg: dict[str, Any] | None = None,
        table_blocks: list[list[dict]],
        section_id: str,
    ) -> list[dict[str, Any]]:
        schemas = section.get("table_schemas")
        if (not isinstance(schemas, list) or not schemas) and cfg:
            schemas = cfg.get("table_schemas")
        if not isinstance(schemas, list) or not schemas:
            return []

        issues: list[dict[str, Any]] = []
        for index, schema in enumerate(schemas):
            if not isinstance(schema, dict):
                continue
            expected = [
                str(header).strip()
                for header in schema.get("headers", [])
                if str(header).strip()
            ]
            if not expected or index >= len(table_blocks):
                continue
            expected_set = set(expected)
            rows = table_blocks[index]
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict):
                    issues.append(cls._backfill_issue(
                        section_id=section_id,
                        message=(
                            f"第 {index + 1} 张表第 {row_index + 1} 行不是 JSON 对象，"
                            "无法按模板表头回填"
                        ),
                        evidence={
                            "table_index": index,
                            "row_index": row_index,
                            "actual_type": type(row).__name__,
                        },
                    ))
                    break
                actual_set = {str(key).strip() for key in row.keys() if str(key).strip()}
                if actual_set != expected_set:
                    issues.append(cls._backfill_issue(
                        section_id=section_id,
                        message=(
                            f"第 {index + 1} 张表字段与模板表头不一致，"
                            f"期望：{'、'.join(expected)}"
                        ),
                        evidence={
                            "table_index": index,
                            "row_index": row_index,
                            "expected_headers": expected,
                            "actual_headers": sorted(actual_set),
                        },
                    ))
                    break
        return issues

    # ── BUG FIX 2026-08-18 (template_backfill) ────────────────────
    # 模板反向规则 handler。检测"模板未声明某字段但 LLM 生成了该字段"。
    # 当前实现覆盖一种反向规则：模板未声明 tables 但 LLM 生成了 tables
    # （用户场景 strategy_2）。后续可扩展到 mode="keep"/"manual" 等。
    #
    # BUG FIX 2026-08-21：审查层必须覆盖 WordExporter 会看到的所有表格
    # 形态。LLM/解析器可能把表格放在 generated_section["content"]，也
    # 可能保留在 generated_section["tables"] 或 content.tables/table 中。
    # 只检查其中一种会让不符合模板的 JSON 漏到 Word 导出阶段才失败。
    @staticmethod
    def _check_template_backfill(
        generated_sections: list[dict],
        rule: dict,
    ) -> list[dict[str, Any]]:
        """模板反向规则 handler。

        模板元数据通过 rule body 传入（handler 签名无法访问
        template_structure）。当前支持的 rule 字段：
          - section_id        : 目标章节稳定标识
          - expected_no_tables : True 表示该章节不应有 tables
        """
        issues: list[dict[str, Any]] = []
        target_id = rule.get("section_id")
        expected_no_tables = bool(rule.get("expected_no_tables", False))
        if not target_id or not expected_no_tables:
            return issues
        for section in generated_sections:
            if not isinstance(section, dict):
                continue
            sid = section.get("section_id") or section.get("id")
            if sid != target_id:
                continue
            table_rows = ResultReviewTool._extract_forbidden_tables(section)
            if table_rows:
                issues.append({
                    "rule_id": rule.get("id", "template_backfill"),
                    "kind": "template_backfill",
                    "severity": rule.get("severity", "block"),
                    "section_id": sid,
                    "message": (
                        "模板未声明该章节的表格占位，但 LLM 返回了 "
                        f"tables 数据（{len(table_rows)} 张表）"
                    ),
                    "evidence": {
                        "table_count": len(table_rows),
                        "table_rows": table_rows,
                    },
                    "span": None,
                })
                break  # 一个 finding per section per rule
        return issues

    @staticmethod
    def _content_is_table(content: Any) -> bool:
        """判断 content 是否为 canonical 表格形态。

        支持:
          - ``list[dict]``            — 单张表（每行一个 dict，键即列名）
          - ``list[list[dict]]``      — 多张表（每个子列表一张表）

        其它形态（``str`` / ``list[str]`` / ``None`` / 空列表）一律视为
        非表格，避免对纯文本章节误报。
        """
        return bool(ResultReviewTool._extract_table_rows(content))

    @staticmethod
    def _extract_table_rows(content: Any) -> list[list[dict]]:
        """把 content 归一化为 ``list[list[dict]]``（多表形式）。

        返回空列表表示该 content 不是表格形态。
        """
        if not isinstance(content, list) or not content:
            return []
        first = content[0]
        if isinstance(first, dict):
            # 单表形态:list[dict]
            return [[row for row in content if isinstance(row, dict)]]
        if isinstance(first, list):
            # 多表形态:list[list[dict]]
            result: list[list[dict]] = []
            for sub in content:
                if isinstance(sub, list) and sub and isinstance(sub[0], dict):
                    result.append([row for row in sub if isinstance(row, dict)])
            return result
        return []

    @staticmethod
    def _extract_forbidden_tables(section: dict[str, Any]) -> list[list[dict]]:
        """Return table-shaped payloads from a no-table-placeholder section.

        WordExporter treats both explicit ``section["tables"]`` and table-shaped
        section content as template-fill data. ResultReviewTool must catch both
        before export so a passed review means the JSON can be backfilled.
        """
        return ResultReviewTool._extract_export_table_blocks(section)

    @classmethod
    def _extract_export_table_blocks(cls, section: dict[str, Any]) -> list[list[dict]]:
        """Extract every table block WordExporter would try to backfill."""
        found: list[list[dict]] = []

        def visit(value: Any) -> None:
            table_rows = cls._extract_table_rows(value)
            if table_rows:
                found.extend(table_rows)
                return
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if isinstance(value, dict):
                main_content = value.get("content")
                if main_content is not None:
                    visit(main_content)
                for key, child in value.items():
                    if key == "content":
                        continue
                    visit(child)

        visit(section.get("tables"))
        visit(section.get("content"))
        return found

    @staticmethod
    def _dedupe_rule_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        no_table_sections: set[str] = set()
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            section_id = str(issue.get("section_id") or "")
            message = str(issue.get("message") or "")
            kind = str(issue.get("kind") or "")
            if (
                kind == "backfill_contract"
                and section_id in no_table_sections
                and "模板未声明" in message
            ):
                continue
            key = (
                kind,
                section_id,
                message,
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
            if kind == "template_backfill" and "模板未声明" in message:
                no_table_sections.add(section_id)
        return deduped
