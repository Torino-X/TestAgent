"""Model output parser and validator — adapted from legacy result_parser.py.

Parses LLM raw text into JSON, sanitises control labels, validates field
completeness (missing / extra / empty / schema-mismatch), supports multi-table
format validation, truncation detection, batch merging, and section-package
construction.

Equivalent migration: all 16 capabilities preserved.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.common.test_plan_schema import CompletenessResult, DEFAULT_SECTIONS


# ── Error ─────────────────────────────────────────────────────────


class ResultParseError(Exception):
    """Raised when model output parsing or validation fails.

    JSON 语法错误、顶层非对象、返回内容为空等**不可定点修复**的错误
    （必须重新生成整个 JSON 才能解决）。
    """


class ResultSchemaMismatch(ResultParseError):
    """表头 / 字段级 schema 不符 — 可定点修复。

    区别于 ``ResultParseError`` 基类：
    * JSON 语法未闭合 → ``ResultParseError``（必须重新生成）
    * 表头改名 / 字段缺失 / 多余字段 / 字段值空 → ``ResultSchemaMismatch``
      （由 ``ResultReviewTool`` + ``RepairAgent`` 定点修复，不必整体重试）

    Phase 2.9A.X：F023 整体重试被替换为 schema_issues 透传机制。
    ``offending_fields`` 字典 key=字段名（与 ``ai_fields[*].field`` 对齐），
    value=问题描述列表（人类可读）或 dict（含 expected / actual）。
    """

    def __init__(
        self, message: str, offending_fields: Dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.offending_fields: Dict[str, Any] = offending_fields or {}


# ── Control-label regexes ─────────────────────────────────────────

# Match status-tag suffixes like "xxx【AI生成】", "xxx（保留原文）"
_CONTROL_LABEL_RE = re.compile(
    r"[【\[\(（]\s*(?:AI\s*生成|保留\s*原文|保留\s*模板\s*原文|手动\s*编辑)\s*[】\]\)）]\s*"
)
# Match status-tag prefixes like "AI生成：xxx", "手动编辑: xxx"
_CONTROL_PREFIX_RE = re.compile(
    r"^\s*(?:AI\s*生成|保留\s*原文|保留\s*模板\s*原文|手动\s*编辑)\s*[：:]\s*"
)


# ── Validation result ─────────────────────────────────────────────


@dataclass
class JsonValidationResult:
    """Field-level validation result for a model JSON output."""

    required_fields: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    extra_fields: List[str] = field(default_factory=list)
    empty_fields: List[str] = field(default_factory=list)
    schema_mismatch_fields: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            not self.missing_fields
            and not self.extra_fields
            and not self.empty_fields
            and not self.schema_mismatch_fields
        )


# ── Parser ────────────────────────────────────────────────────────


class ResultParser:
    """Model output parser: JSON extraction → sanitise → validate → section package."""

    # ── Public API ─────────────────────────────────────────────────

    def parse_and_validate_json(
        self, content: str, config: Dict[str, object] | None
    ) -> Dict[str, Any]:
        """Parse model output and perform full validation.

        Returns the payload dict ordered by ``required_fields`` on success.

        Raises:
            ResultParseError: JSON 语法错 / 顶层非 dict / 空响应（不可定点修复）。
            ResultSchemaMismatch: 表头/字段 schema 不符（可由 ResultReviewTool
                + RepairAgent 定点修复，不必整体重试）。Phase 2.9A.X 起取代
                F023 的整体重试机制。
        """
        payload = self.sanitize_payload(self.parse_json(content))
        result = self.validate_json_payload(payload, config)
        if result.passed:
            return self._ordered_payload(payload, result.required_fields)

        problems = []
        offending_fields: Dict[str, Any] = {}
        if result.missing_fields:
            problems.append("缺少字段：" + "、".join(result.missing_fields))
            for f in result.missing_fields:
                offending_fields[f] = {"kind": "missing_field", "field": f}
        if result.extra_fields:
            problems.append("不允许的字段：" + "、".join(result.extra_fields))
            for f in result.extra_fields:
                offending_fields[f] = {"kind": "extra_field", "field": f}
        if result.empty_fields:
            problems.append("内容为空字段：" + "、".join(result.empty_fields))
            for f in result.empty_fields:
                offending_fields[f] = {"kind": "empty_field", "field": f}
        if result.schema_mismatch_fields:
            problems.append(
                "结构不符合模板字段：" + "；".join(result.schema_mismatch_fields)
            )
            # schema_mismatch_fields 是形如 "section_a xxx" 的消息列表，
            # 字段名是首段标识符（字母数字下划线），后面是中文描述。
            # 没有分隔符可依赖，用正则取首段标识符。
            import re as _re
            for line in result.schema_mismatch_fields:
                m = _re.match(r"^[\w]+", line)
                field = m.group(0) if m else line
                existing = offending_fields.get(field)
                if existing is None:
                    offending_fields[field] = {
                        "kind": "schema_mismatch",
                        "field": field,
                        "messages": [line],
                    }
                elif isinstance(existing, dict):
                    existing.setdefault("messages", []).append(line)
        raise ResultSchemaMismatch(
            "模型 JSON 输出校验失败，" + "；".join(problems),
            offending_fields=offending_fields,
        )

    def format_json(self, payload: Dict[str, Any]) -> str:
        """Pretty-print a payload as indented JSON (for preview display)."""
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # ── JSON extraction ────────────────────────────────────────────

    def parse_json(self, content: str) -> Dict[str, Any]:
        """Extract a JSON object from raw model output.

        Handles Markdown fences, bracket-matching fallback, and whitespace
        around key names (e.g. ``" overview"`` → ``"overview"``).
        Raises ``ResultParseError`` with diagnostic information on failure.
        """
        raw_text = content.strip()
        total_length = len(raw_text)
        if not raw_text:
            raise ResultParseError("模型返回内容为空（可能因输出长度限制导致截断）")

        text = self._strip_markdown_fence(raw_text)
        fence_stripped = text != raw_text
        if not text:
            raise ResultParseError(
                f"去除 Markdown 围栏后内容为空，可能因输出长度限制导致截断。"
                f"原始长度：{total_length} 字符"
            )

        # 2026-07：删除了 `"\s+...\s*"` 的 key 空白"归一化"正则。
        # 旧逻辑不分 JSON key 和字符串 value，会把 `"审批意见": "...",`
        # 后面那个 `,` 与 `"审批意见"` 的左引号一起吃掉，中文密集响应
        # 在第二个键之后结构就塌方。改由 prompt（full_rules.md 第 10 条）
        # 显式禁止键名带空白，parser 不再修复键名。
        diagnostic = (
            f"响应总长度：{total_length} 字符"
            + (f"，去除 Markdown 围栏后：{len(text)} 字符" if fence_stripped else "")
        )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as first_error:
            try:
                json_text = self._extract_json_object(text)
            except ResultParseError as extract_error:
                tail = text[-200:] if len(text) > 200 else text
                raise ResultParseError(
                    f"模型返回内容中无法提取完整 JSON 对象（可能因输出长度限制导致截断）。"
                    f"{diagnostic}\n"
                    f"JSON 起始位置：{text.find('{')}，末尾 200 字符：…{tail}"
                ) from extract_error

            try:
                payload = json.loads(json_text)
            except json.JSONDecodeError as exc:
                line = exc.lineno
                col = exc.colno
                pos = exc.pos
                snippet_start = max(0, pos - 120) if pos else 0
                snippet_end = (
                    min(len(json_text), pos + 120) if pos else min(500, len(json_text))
                )
                ctx = json_text[snippet_start:snippet_end]
                # 2026-07：标签从「位置比例」改成「真实截断信号」。
                # 旧逻辑 `pos > len*0.8` 误把结构破坏（如正则吃引号）
                # 也归到「截断」，掩盖真问题。改用 is_json_truncated 二次
                # 判断，只有确实未闭合时才提示截断。
                truncated = self.is_json_truncated(json_text)
                hint = (
                    "（可能因输出长度限制导致截断）" if truncated
                    else "（响应结构异常，非截断）"
                )
                raise ResultParseError(
                    f"模型返回的 JSON 不合法：{exc.msg}（第 {line} 行，第 {col} 列）"
                    f"{hint}。"
                    f"{diagnostic}\n"
                    f"错误附近内容：…{ctx}…"
                ) from exc

        if not isinstance(payload, dict):
            raise ResultParseError("模型 JSON 顶层必须是对象")
        return payload

    # ── Validation ─────────────────────────────────────────────────

    def validate_json_payload(
        self, payload: Dict[str, Any], config: Dict[str, object] | None
    ) -> JsonValidationResult:
        """Validate field completeness: missing / extra / empty / schema match."""
        required_fields = self.required_fields(config)
        actual_fields = list(payload.keys())
        required_set = set(required_fields)
        actual_set = set(actual_fields)

        missing_fields = [f for f in required_fields if f not in actual_set]
        has_config = config is not None
        extra_fields = [
            f for f in actual_fields if has_config and f not in required_set
        ]
        empty_fields = [
            f
            for f in required_fields
            if f in payload and self._is_empty_value(payload[f])
        ]
        bindings = self.field_bindings(config)
        schema_mismatch_fields = [
            problem
            for f in required_fields
            if f in payload
            for problem in self._validate_field_schema(f, payload[f], bindings.get(f, {}))
        ]

        return JsonValidationResult(
            required_fields=required_fields,
            missing_fields=missing_fields,
            extra_fields=extra_fields,
            empty_fields=empty_fields,
            schema_mismatch_fields=schema_mismatch_fields,
        )

    def required_fields(self, config: Dict[str, object] | None) -> List[str]:
        """Extract the ordered list of AI-generated field names from config."""
        if not config:
            return []
        ai_fields = config.get("ai_fields", [])
        fields: List[str] = []
        if isinstance(ai_fields, list):
            for item in ai_fields:
                if isinstance(item, dict):
                    name = str(item.get("field", "")).strip()
                    if name:
                        fields.append(name)
        return fields

    def field_bindings(
        self, config: Dict[str, object] | None
    ) -> Dict[str, Dict[str, Any]]:
        """Build field-name → binding-info map.

        Prefers ``field_bindings`` key in config; falls back to building from
        ``ai_fields``.
        """
        if not config:
            return {}
        raw = config.get("field_bindings", {})
        if isinstance(raw, dict):
            bindings = {
                str(k): v for k, v in raw.items() if isinstance(v, dict)
            }
            if bindings:
                return bindings

        bindings: Dict[str, Dict[str, Any]] = {}
        ai_fields = config.get("ai_fields", [])
        if isinstance(ai_fields, list):
            for item in ai_fields:
                if isinstance(item, dict):
                    name = str(item.get("field", "")).strip()
                    if name:
                        bindings[name] = item
        return bindings

    def check_completeness(self, content: str) -> CompletenessResult:
        """Quick check: does the output mention all default required sections?"""
        normalized = content.replace(" ", "")
        missing = [s for s in DEFAULT_SECTIONS if s not in normalized]
        return CompletenessResult(
            required_sections=DEFAULT_SECTIONS.copy(), missing_sections=missing
        )

    # ── Truncation detection ───────────────────────────────────────

    def is_json_truncated(self, content: str) -> bool:
        """Detect whether a model JSON response was truncated.

        Scans brace nesting after stripping markdown fences.  Returns True if
        the outermost ``{`` never closes.
        """
        text = self._strip_markdown_fence(content.strip())
        start = text.find("{")
        if start < 0:
            return False

        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return False  # properly closed
        return True  # never closed

    def parse_json_lenient(
        self, content: str, config: Dict[str, object] | None = None
    ) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]]]:
        """Best-effort 宽容解析 JSON — 用于 LLM 输出截断的修复路径。

        流程：
        1. ``json.JSONDecoder().raw_decode(content)`` 取最大可解析前缀
        2. raw_decode 失败时调 ``_slice_and_parse``（按 brace 深度扫描找
           最后一个顶层闭合点切片再 raw_decode）
        3. 比对 ``config.ai_fields[*].field`` 期望字段集合：
           - raw_decode 拿到 partial：缺失的字段 emit missing_field issue
           - 两者都失败（JSON 严重损坏）：返回 ``(None, [...所有字段 missing...])``
             由 ResultReviewTool + RepairAgent 走全量补生成路径

        Args:
            content: LLM 原始输出（含 markdown fence 也 OK）
            config: ``generation_config`` 字典；为 None 时只做解析不发 issue

        Returns:
            ``(partial_payload, schema_issues)`` 元组：
              - ``partial_payload``: best-effort 解析出的 dict（顶层非 dict 或
                完全损坏时为 None）
              - ``schema_issues``: list[{kind, field, severity, message}, ...]
                kind 取 ``missing_field``，severity 固定 ``"block"``

        Raises:
            ``ResultParseError`` 当响应去除 Markdown 围栏后为空——这种情况
            模型根本没有任何 JSON 输出，必须整体重新生成。
        """
        text = self._strip_markdown_fence(content.strip())
        if not text:
            raise ResultParseError(
                "宽容解析失败：去除 Markdown 围栏后内容为空"
            )

        decoder = json.JSONDecoder()
        parsed: Any | None = None
        try:
            parsed, _end = decoder.raw_decode(text)
        except json.JSONDecodeError:
            # raw_decode 失败 — 尝试 slice 兜底（找最后一个顶层闭合点切片）
            parsed = self._slice_and_parse(text)

        # 严格只接受 dict 顶层
        if parsed is not None and not isinstance(parsed, dict):
            parsed = None

        # 比对期望字段，emit 缺失项
        schema_issues: List[Dict[str, Any]] = []
        if config is not None:
            required_fields = self.required_fields(config)
            if parsed is None:
                # JSON 完全损坏 — 所有期望字段都按 missing_field 处理
                for field_name in required_fields:
                    schema_issues.append({
                        "kind": "missing_field",
                        "field": field_name,
                        "severity": "block",
                        "message": (
                            f"宽容解析完全失败，字段「{field_name}」未生成，"
                            f"将由 RepairAgent 定点补生成"
                        ),
                    })
            else:
                actual_set = set(parsed.keys())
                for field_name in required_fields:
                    if field_name not in actual_set:
                        schema_issues.append({
                            "kind": "missing_field",
                            "field": field_name,
                            "severity": "block",
                            "message": (
                                f"宽容解析后字段「{field_name}」仍缺失，"
                                f"将由 RepairAgent 定点补生成"
                            ),
                        })

        return parsed, schema_issues

    @staticmethod
    def _slice_and_parse(text: str) -> Any | None:
        """raw_decode 失败兜底：扫一遍找最右的"顶层闭合点"切片再 raw_decode。

        ``is_json_truncated`` 的逆向——找出深度最后一次回到 0 的位置，即
        第一个 ``{`` 与之匹配的 ``}`` 的位置；可能存在多个嵌套闭合点，我们
        取**最右一个**作为切片终点，丢弃其后的所有尾部未闭合内容。

        仅处理最常见的两种截断形态：
        * 截断发生在某个章节闭合后、下一章节未开始
        * 截断发生在某章节内（尾部未闭合），丢弃未闭合部分

        仍然失败的 case 抛 ``ResultParseError`` 给上层。
        """
        decoder = json.JSONDecoder()
        text = text.strip()
        start = text.find("{")
        if start < 0:
            return None

        # 走一遍，记录所有 depth 回到 0 的位置
        depth = 0
        in_string = False
        escape = False
        last_balanced_end: int | None = None
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    last_balanced_end = i
        if last_balanced_end is None:
            return None

        candidate = text[: last_balanced_end + 1]
        try:
            parsed, _end = decoder.raw_decode(candidate)
            return parsed
        except json.JSONDecodeError:
            return None

    # ── Sanitisation ───────────────────────────────────────────────

    def sanitize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively strip control labels from all values in the payload."""
        return {key: self._sanitize_value(value) for key, value in payload.items()}

    # ── Section package ────────────────────────────────────────────

    def build_section_package(
        self, payload: Dict[str, Any], config: Dict[str, object] | None
    ) -> Dict[str, Any]:
        """Merge a validated JSON payload with template binding info to produce
        a section-package consumable by WordExporter.
        """
        ordered = self._ordered_payload(payload, self.required_fields(config))
        field_bindings = self.field_bindings(config)

        generated_sections = []
        for field_name, value in ordered.items():
            binding = field_bindings.get(
                field_name, {"field": field_name, "title": field_name}
            )
            generated_sections.append(
                {
                    "field": field_name,
                    "section_id": binding.get("section_id", ""),
                    "title": binding.get("title", field_name),
                    "clean_title": binding.get(
                        "clean_title", binding.get("title", field_name)
                    ),
                    "level": binding.get("level"),
                    "order": binding.get("order"),
                    "path": binding.get("path", []),
                    "paragraph_index": binding.get("paragraph_index"),
                    "body_start_index": binding.get("body_start_index"),
                    "body_end_index": binding.get("body_end_index"),
                    "table_indexes": binding.get("table_indexes", []),
                    "table_schemas": binding.get("table_schemas", []),
                    "source": binding.get("source", ""),
                    "target_kind": binding.get("target_kind", "leaf_section"),
                    "content": value,
                }
            )

        return {
            "schema_version": 1,
            "payload": ordered,
            "generated_sections": generated_sections,
            "keep_sections": self._list_config(config, "keep_sections"),
            "manual_sections": self._list_config(config, "manual_sections"),
            "section_bindings": self._list_config(config, "section_bindings"),
        }

    # ── Batch merge ────────────────────────────────────────────────

    def merge_batch_payloads(
        self,
        batch_payloads: List[Dict[str, Any]],
        config: Dict[str, object] | None,
    ) -> Dict[str, Any]:
        """Merge per-batch payloads into one complete payload.

        Validates: no duplicate keys across batches, no missing required fields.
        """
        if not batch_payloads:
            raise ResultParseError("没有可合并的批次结果")

        merged: Dict[str, Any] = {}
        seen: set = set()

        for idx, payload in enumerate(batch_payloads):
            if not isinstance(payload, dict):
                raise ResultParseError(f"批次 {idx + 1} 的结果不是 JSON 对象")
            for key in payload:
                if key in seen:
                    raise ResultParseError(
                        f"字段 '{key}' 在多个批次中重复出现，请检查批次拆分逻辑"
                    )
                seen.add(key)
            merged.update(payload)

        required = self.required_fields(config)
        actual = set(merged.keys())
        missing = [f for f in required if f not in actual]
        if missing:
            raise ResultParseError(f"合并后缺少字段：{'、'.join(missing)}")

        return merged

    # ── Internal helpers ───────────────────────────────────────────

    @staticmethod
    def _ordered_payload(
        payload: Dict[str, Any], required_fields: List[str]
    ) -> Dict[str, Any]:
        """Reorder payload keys to match ``required_fields`` order."""
        if not required_fields:
            return payload
        ordered: Dict[str, Any] = {}
        for fn in required_fields:
            if fn in payload:
                ordered[fn] = payload[fn]
        return ordered

    @staticmethod
    def _list_config(config: Dict[str, object] | None, key: str) -> List[Any]:
        """Safely read a list-valued config key."""
        if not config:
            return []
        val = config.get(key, [])
        return val if isinstance(val, list) else []

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        """Remove a single surrounding `` ```json ... ``` `` fence."""
        m = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", text, re.S)
        if m:
            return m.group(1).strip()
        return text

    @staticmethod
    def _extract_json_object(text: str) -> str:
        """Extract the first complete JSON object via brace-depth scan."""
        start = text.find("{")
        if start < 0:
            raise ResultParseError("模型返回内容中未找到 JSON 对象")

        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        raise ResultParseError("模型返回内容中的 JSON 对象不完整")

    @staticmethod
    def _is_empty_value(value: Any) -> bool:
        """Check whether a value is semantically empty."""
        if value is None:
            return True
        if isinstance(value, str):
            n = value.strip()
            return not n or n in {"...", "…", "待补充", "暂无", "无", "N/A", "n/a"}
        if isinstance(value, list):
            return not value or all(
                ResultParser._is_empty_value(item) for item in value
            )
        if isinstance(value, dict):
            return not value or all(
                ResultParser._is_empty_value(item) for item in value.values()
            )
        return False

    # ── Value-level sanitisation ───────────────────────────────────

    @classmethod
    def _sanitize_value(cls, value: Any) -> Any:
        """Recursively strip control labels from str / list / dict."""
        if isinstance(value, str):
            return cls._sanitize_text(value)
        if isinstance(value, list):
            return [cls._sanitize_value(item) for item in value]
        if isinstance(value, dict):
            return {k: cls._sanitize_value(item) for k, item in value.items()}
        return value

    @staticmethod
    def _sanitize_text(text: str) -> str:
        """Strip prefix and suffix control labels from a single string."""
        cleaned = _CONTROL_LABEL_RE.sub("", text)
        cleaned = _CONTROL_PREFIX_RE.sub("", cleaned)
        return cleaned.strip()

    # ── Field schema validation ────────────────────────────────────

    @staticmethod
    def _validate_field_schema(
        field_name: str, value: Any, binding: Dict[str, Any]
    ) -> List[str]:
        """Validate table-field structure against template table schemas.

        Supports two formats:
        - **New (multi-table)**: ``[[{…}, {…}], [{…}]]`` — outer list per table
        - **Legacy (single-table)**: ``[{…}]`` — auto-converted to ``[[{…}]]``
        """
        table_schemas = binding.get("table_schemas", [])
        if not isinstance(table_schemas, list) or not table_schemas:
            if ResultParser._contains_table_value(value):
                return [
                    f"{field_name} 对应的模板章节未声明表格占位，"
                    "不得返回对象数组、嵌套数组或其他表格数据"
                ]
            return []

        # Detect and convert legacy single-table format
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
        ):
            value = [value]

        if not isinstance(value, list) or not value:
            return [f"{field_name} 应返回数组格式"]

        if len(table_schemas) > 1 and len(value) != len(table_schemas):
            return [
                f"{field_name} 应返回 {len(table_schemas)} 个表格的数据，"
                f"实际返回了 {len(value)} 个"
            ]

        problems: List[str] = []

        for table_idx, schema in enumerate(table_schemas):
            if not isinstance(schema, dict):
                continue

            headers = [
                str(h) for h in schema.get("headers", []) if str(h).strip()
            ]
            if not headers:
                continue

            if table_idx >= len(value):
                problems.append(
                    f"{field_name} 缺少第 {table_idx + 1} 个表格的数据"
                )
                continue

            table_data = value[table_idx]
            if not isinstance(table_data, list) or not table_data:
                problems.append(
                    f"{field_name} 第 {table_idx + 1} 个表格应返回非空对象数组"
                )
                continue

            if not all(isinstance(item, dict) for item in table_data):
                problems.append(
                    f"{field_name} 第 {table_idx + 1} 个表格应返回对象数组，"
                    f"字段必须对应模板表头：{'、'.join(headers)}"
                )
                continue

            header_set = set(headers)
            for row_idx, row in enumerate(table_data, start=1):
                row_keys = {str(k) for k in row.keys()}
                missing = [h for h in headers if h not in row_keys]
                extra = [k for k in row_keys if k not in header_set]
                if missing:
                    problems.append(
                        f"{field_name} 第 {table_idx + 1} 个表格第 {row_idx} 行"
                        f"缺少表头：{'、'.join(missing)}"
                    )
                if extra:
                    problems.append(
                        f"{field_name} 第 {table_idx + 1} 个表格第 {row_idx} 行"
                        f"包含非模板表头：{'、'.join(extra)}"
                    )

        return problems

    @staticmethod
    def _contains_table_value(value: Any) -> bool:
        """Mirror the Word renderer's table-shape detection without importing it.

        ``WordExporter._content_to_blocks`` renders a non-empty list of dicts
        as a table, including one nested inside a dict.  A section with no
        template table placeholder must reject that shape before repair or
        export, otherwise a late ``WordExportError`` is inevitable.
        """
        if isinstance(value, list):
            if value and all(isinstance(item, dict) for item in value):
                return True
            return any(ResultParser._contains_table_value(item) for item in value)
        if isinstance(value, dict):
            return any(ResultParser._contains_table_value(item) for item in value.values())
        return False


# 模块定位:Model output parser + validator(759 行,LLM 输出端核心)
#
# 能力:
#   - parse_and_validate_json(raw_text)
#     处理 Markdown ```json 围栏 + bracket-matched + truncation + continuation
#   - sanitize_control_label(section)
#     去除 control 字符,防 LLM 注入
#   - validate_field_completeness(payload, schema)
#     校验缺失 / 多余 / 空 / schema-mismatch
#   - build_section_package(...)
#     构造 phase 输出的 section_package
#
# 链路:
#   TestPlanGeneratorTool / TestPlanRegenTool
#     → LLMClient.generate_with_system(...) → raw text →
#       result_parser.parse_and_validate_json(raw_text) → dict
#     → 校验后写 state.test_plan_content
#
# 关键约束:
#   - 失败抛 JSON_VALIDATION_FAILED,与 TestPlanGeneratorTool 的
#     internal retry 机制配合
#   - 不调 LLM(只用启发式 + schema)
#   - schema 来自 template_section_service.build_review_standard
#   - 不允许 log raw_text(可能含私有内容)
