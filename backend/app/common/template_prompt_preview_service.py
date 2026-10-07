"""
模板提示词预览服务（从 legacy_sources 等价迁移）。

提供章节到英文字段名的映射、JSON schema 预览生成、生成配置构建。

与旧项目的差异（架构适配）：
- 日志改为标准 logging
- 输出结构不变，直接返回 dict
- 完全保留 FIELD_MAP 映射表、fallback 命名逻辑、占位符生成逻辑
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List

from app.common.template_section import TemplateSection

logger = logging.getLogger(__name__)


class TemplatePromptPreviewService:
    """模板章节处理配置服务。

    负责：
    - 中文标题 → 英文字段名映射
    - JSON schema 预览生成
    - 生成配置（ai_fields / keep_sections / manual_sections / field_bindings）构建
    """

    # 字段映射（中文标题关键词 → 英文字段名）
    FIELD_MAP: Dict[str, str] = {
        "项目概述": "overview",
        "测试目标": "objectives",
        "测试范围": "scope",
        "测试策略": "strategy",
        "测试进度": "schedule",
        "准入准出标准": "entry_exit_criteria",
        "风险分析": "risks",
        "测试交付物": "deliverables",
    }

    def build_json_preview(self, sections: List[TemplateSection]) -> str:
        """根据章节树生成 JSON schema 预览字符串。"""
        specs = self.ai_field_specs(sections)
        if not specs:
            payload = {}
        else:
            payload = {item["field"]: self._placeholder_for(item) for item in specs}
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def build_generation_config(self, sections: List[TemplateSection]) -> Dict[str, object]:
        """构建完整的生成配置，包含 ai_fields、keep_sections、manual_sections 和 section_bindings。"""
        section_bindings = self.section_bindings(sections)
        ai_specs = [
            self._field_spec_from_binding(binding)
            for binding in section_bindings
            if self._is_fillable_ai_binding(binding)
        ]
        keep_sections = [section.title for section in self._walk(sections) if section.mode == "keep"]
        manual_sections = [section.title for section in self._walk(sections) if section.mode == "manual"]
        return {
            "template_map_version": 1,
            "ai_fields": ai_specs,
            "keep_sections": keep_sections,
            "manual_sections": manual_sections,
            "section_bindings": section_bindings,
            "field_bindings": {item["field"]: item for item in ai_specs},
            "json_schema_preview": self.build_json_preview(sections),
        }

    def ai_sections(self, sections: List[TemplateSection]) -> List[TemplateSection]:
        """返回所有 mode='ai' 的章节。"""
        return [section for section in self._walk(sections) if section.mode == "ai"]

    def ai_fields(self, sections: List[TemplateSection]) -> List[str]:
        """返回所有 AI 生成章节的字段名列表。"""
        return [item["field"] for item in self.ai_field_specs(sections)]

    def ai_field_specs(self, sections: List[TemplateSection]) -> List[Dict[str, object]]:
        """返回所有 AI 填充字段的规格列表。"""
        return [
            self._field_spec_from_binding(binding)
            for binding in self.section_bindings(sections)
            if self._is_fillable_ai_binding(binding)
        ]

    def section_bindings(self, sections: List[TemplateSection]) -> List[Dict[str, object]]:
        """为所有章节（包括子章节）生成绑定信息列表。"""
        bindings: List[Dict[str, object]] = []
        used_fields: set[str] = set()

        def visit(items: List[TemplateSection], parent_path: List[str]) -> None:
            for section in items:
                order = len(bindings) + 1
                section_id = self._section_id(section, order)
                field = self.section_to_field(
                    section.title, order, section_id=section_id,
                )
                field = self._dedupe_field(field, used_fields)
                used_fields.add(field)
                path = parent_path + [section.title]
                bindings.append(self._section_binding(section, order, path, field))
                if section.children:
                    visit(section.children, path)

        visit(sections, [])
        return bindings

    def section_to_field(
        self, title: str, fallback_index: int = 1, *,
        section_id: str | None = None,
    ) -> str:
        """将中文章节标题映射为英文字段名。

        Args:
            title: 中文章节标题（如 "2 测试目标" 或 "审批信息"）。
            fallback_index: 顺序编号（仅在没有任何稳定标识时兜底使用）。
            section_id: 章节稳定标识（``body_X_level_Y`` / ``section_X_level_Y``），
                用于既无数字前缀也无 ASCII 词的中文标题（如"审批信息"）。
                必须传入以保证 field 名跨模板顺序变更时仍稳定。
        """
        for keyword, field in self.FIELD_MAP.items():
            if keyword in title:
                return field
        return self._fallback_field_name(title, fallback_index, section_id=section_id)

    # ── 内部方法 ─────────────────────────────────────────────────

    @staticmethod
    def _section_binding(
        section: TemplateSection,
        order: int,
        path: List[str],
        field: str,
    ) -> Dict[str, object]:
        return {
            "section_id": TemplatePromptPreviewService._section_id(section, order),
            "field": field if section.mode == "ai" else "",
            "suggested_field": field,
            "title": section.title,
            "clean_title": TemplatePromptPreviewService._clean_title(section.title),
            "level": section.level,
            "mode": section.mode,
            "checked": section.checked,
            "has_children": bool(section.children),
            "child_count": len(section.children),
            "order": order,
            "path": path,
            "paragraph_index": section.paragraph_index,
            "body_start_index": section.body_start_index,
            "body_end_index": section.body_end_index,
            "table_indexes": section.table_indexes,
            "table_schemas": section.table_schemas,
            "source": section.source,
            "description": section.description,
        }

    @staticmethod
    def _field_spec_from_binding(binding: Dict[str, object]) -> Dict[str, object]:
        return {
            "field": str(binding["field"]),
            "title": str(binding["title"]),
            "section_id": str(binding["section_id"]),
            "clean_title": str(binding["clean_title"]),
            "level": int(binding["level"]),
            "order": int(binding["order"]),
            "path": list(binding["path"]),
            "paragraph_index": binding["paragraph_index"],
            "body_start_index": binding["body_start_index"],
            "body_end_index": binding["body_end_index"],
            "table_indexes": list(binding["table_indexes"]),
            "table_schemas": list(binding.get("table_schemas", [])),
            "source": str(binding["source"]),
            "description": str(binding.get("description", "")),
            "target_kind": "leaf_section",
        }

    @staticmethod
    def _is_fillable_ai_binding(binding: Dict[str, object]) -> bool:
        return binding["mode"] == "ai" and not bool(binding.get("has_children"))

    @staticmethod
    def _section_id(section: TemplateSection, order: int) -> str:
        if section.body_start_index is not None:
            return f"body_{section.body_start_index}_level_{section.level}"
        if section.paragraph_index is not None:
            return f"para_{section.paragraph_index}_level_{section.level}"
        return f"section_{order}_level_{section.level}"

    @staticmethod
    def _clean_title(title: str) -> str:
        return title.split(" ", 1)[1] if " " in title and title.split(" ", 1)[0][0].isdigit() else title

    @staticmethod
    def _fallback_field_name(
        title: str, fallback_index: int, *,
        section_id: str | None = None,
    ) -> str:
        """根据标题编号或英文单词生成后备字段名。

        既无数字前缀也无 ASCII 词的纯中文标题（如"审批信息"）会落到最后一个
        分支：必须用 ``section_id`` 派生稳定标识，**禁止**用 ``fallback_index``
        （binding 列表长度随遍历顺序变化，会导致同一章节在不同模板位置得到
        不同的 field 名，破坏 schema 校验与 RepairAgent section_id 路由）。
        """
        number = re.match(r"^\s*(\d+(?:\.\d+)*)", title)
        if number:
            return "section_" + number.group(1).replace(".", "_")
        ascii_words = re.findall(r"[A-Za-z][A-Za-z0-9]*", title)
        if ascii_words:
            return "_".join(word.lower() for word in ascii_words[:5])
        if section_id:
            return f"field_{section_id}"
        return f"field_{fallback_index}"

    @staticmethod
    def _dedupe_field(field: str, used_fields: set[str]) -> str:
        """去重：若字段名已被使用，追加 _2/_3... 后缀。"""
        if field not in used_fields:
            return field
        suffix = 2
        while f"{field}_{suffix}" in used_fields:
            suffix += 1
        return f"{field}_{suffix}"

    def _placeholder_for(self, item: Dict[str, object]) -> object:
        """根据 table_schemas 和 field 类型生成占位 JSON。"""
        table_schemas = item.get("table_schemas", [])
        if isinstance(table_schemas, list) and table_schemas:
            if len(table_schemas) == 1:
                # 单表格：保持原有格式（一维数组）
                first_table = table_schemas[0]
                if isinstance(first_table, dict):
                    headers = first_table.get("headers", [])
                    if isinstance(headers, list) and headers:
                        return [{str(header): "..." for header in headers}]
            else:
                # 多表格：返回二维数组，外层每个元素对应一个表格
                result = []
                for schema in table_schemas:
                    if isinstance(schema, dict):
                        headers = schema.get("headers", [])
                        if isinstance(headers, list) and headers:
                            result.append([{str(header): "..." for header in headers}])
                        else:
                            result.append(["..."])
                    else:
                        result.append(["..."])
                return result

        field = str(item.get("field", ""))
        if field in {"overview", "scope", "entry_exit_criteria"}:
            return {"content": "..."}
        return ["..."]

    @staticmethod
    def _walk(sections: List[TemplateSection]) -> List[TemplateSection]:
        """递归展平树形章节列表。"""
        result: List[TemplateSection] = []
        for section in sections:
            result.extend(section.walk())
        return result


# 模块定位:模板提示词预览服务
#
# 提供:
#   - 章节 → 英文字段名映射;
#   - JSON schema 预览(给前端展示);
#   - 生成配置 build_review_standard(给 ResultReviewTool 用)。
#
# 链路:
#   TemplateParserTool.run → template_structure dict →
#     TemplatePromptPreviewService.preview(schema) → 给前端 + 给 review_standard
#
# 关键约束:
#   - 是模板解析后的下游,**只在 template_structure 已存在时调用**;
#   - 输出是 dict,**不**调 LLM,无副作用;
#   - 字段映射规则是测试覆盖硬性要求(改了要带 mock 测试);
#   - 不要加 UI 逻辑(渲染由前端处理)。
