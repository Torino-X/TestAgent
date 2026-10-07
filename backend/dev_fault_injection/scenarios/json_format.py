"""JSON format fault scenarios for ResultReviewTool/RepairAgent testing."""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from app.fault_injection_gateway import POINT_AFTER_TEST_PLAN_LLM_RAW_JSON

logger = logging.getLogger(__name__)

TEXT_CONTENT_KEYS = (
    "content",
    "body",
    "text",
    "summary",
    "description",
    "value",
    "正文",
    "内容",
    "说明",
)


class _JsonScenario:
    point = POINT_AFTER_TEST_PLAN_LLM_RAW_JSON

    def _load(self, raw_json: str) -> dict[str, Any]:
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            raise ValueError("raw_json must decode to object")
        return payload

    def _dump(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _ai_fields(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        config = context.get("generation_config") or {}
        fields = config.get("ai_fields", []) if isinstance(config, dict) else []
        return [field for field in fields if isinstance(field, dict)]

    def _field_name(self, field: dict[str, Any]) -> str | None:
        value = field.get("field")
        return str(value) if value else None

    def _declared_tables(self, field: dict[str, Any]) -> int:
        schemas = field.get("table_schemas")
        indexes = field.get("table_indexes")
        if isinstance(schemas, list):
            return len(schemas)
        if isinstance(indexes, list):
            return len(indexes)
        return 0

    def _headers_for(self, field: dict[str, Any]) -> list[str]:
        schemas = field.get("table_schemas")
        if not isinstance(schemas, list) or not schemas:
            return ["字段A", "字段B"]
        schema = schemas[0]
        if not isinstance(schema, dict):
            return ["字段A", "字段B"]
        headers = schema.get("headers")
        if isinstance(headers, list) and headers:
            return [str(header) for header in headers]
        return ["字段A", "字段B"]

    def _choose_field(
        self,
        payload: dict[str, Any],
        context: dict[str, Any],
        predicate,
    ) -> tuple[str, dict[str, Any] | None]:
        for field in self._ai_fields(context):
            name = self._field_name(field)
            if name and name in payload and predicate(field):
                return name, field
        first_key = next(iter(payload.keys()), "")
        return first_key, None

    def _iter_rows(self, value: Any) -> Iterable[dict[str, Any]]:
        if not isinstance(value, list):
            return
        for row in value:
            if isinstance(row, dict):
                yield row
            elif isinstance(row, list):
                for inner in row:
                    if isinstance(inner, dict):
                        yield inner


class SchemaHeaderMismatchScenario(_JsonScenario):
    key = "result_review.schema_header_mismatch"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        for section_id, value in data.items():
            for row in self._iter_rows(value):
                headers = list(row.keys())
                if not headers:
                    continue
                original = headers[-1]
                row["TEST_FAULT_" + original] = row.pop(original)
                logger.info(
                    "FAULT_INJECTION: corrupted header %s in %s",
                    original,
                    section_id,
                )
                return self._dump(data)
        logger.warning("FAULT_INJECTION: no table row found for header mismatch")
        return payload


class MissingSectionScenario(_JsonScenario):
    key = "result_review.missing_section"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        keys = list(data.keys())
        if len(keys) >= 3:
            del data[keys[-2]]
            return self._dump(data)
        if len(keys) == 2:
            del data[keys[-1]]
            return self._dump(data)
        return payload


class JsonTruncateScenario(_JsonScenario):
    key = "result_review.json_truncate"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        cut = int(len(payload) * 0.66)
        if cut < 10:
            return payload
        return payload[:cut]


class EmptySectionContentScenario(_JsonScenario):
    key = "result_review.empty_section_content"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        fields = self._ai_fields(context)
        preferred_fields = [field for field in fields if self._declared_tables(field) == 0]
        for candidates in (preferred_fields, fields):
            for field in candidates:
                name = self._field_name(field)
                if not name or name not in data:
                    continue
                if self._clear_section_content(data, name):
                    logger.info("FAULT_INJECTION: emptied section content in %s", name)
                    return self._dump(data)
        logger.warning("FAULT_INJECTION: no suitable section found for empty content")
        return payload

    def _clear_section_content(self, data: dict[str, Any], name: str) -> bool:
        value = data.get(name)
        if isinstance(value, str):
            if not value.strip():
                return False
            data[name] = ""
            return True

        if isinstance(value, dict):
            if self._clear_dict_content(value):
                return True
            data[name] = {"content": ""}
            return True

        if isinstance(value, list):
            if self._clear_list_content(value):
                return True
            if value:
                data[name] = []
                return True

        return False

    def _clear_dict_content(self, value: dict[str, Any]) -> bool:
        for key in TEXT_CONTENT_KEYS:
            current = value.get(key)
            if isinstance(current, str) and current.strip():
                value[key] = ""
                return True

        string_candidates = {
            key: current
            for key, current in value.items()
            if isinstance(current, str) and current.strip()
        }
        if string_candidates:
            longest_key = max(
                string_candidates,
                key=lambda candidate: len(string_candidates[candidate]),
            )
            value[longest_key] = ""
            return True

        for current in value.values():
            if isinstance(current, dict) and self._clear_dict_content(current):
                return True
            if isinstance(current, list) and self._clear_list_content(current):
                return True
        return False

    def _clear_list_content(self, value: list[Any]) -> bool:
        for item in value:
            if isinstance(item, dict) and self._clear_dict_content(item):
                return True
            if isinstance(item, list) and self._clear_list_content(item):
                return True

        for row in self._iter_rows(value):
            candidates = {
                key: current
                for key, current in row.items()
                if isinstance(current, str) and current.strip()
            }
            if not candidates:
                continue
            longest_key = max(candidates, key=lambda key: len(candidates[key]))
            row[longest_key] = ""
            return True
        return False


class SectionInvalidValueScenario(_JsonScenario):
    key = "result_review.section_invalid_value"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        first_key = next(iter(data.keys()), None)
        if first_key is None:
            return payload
        data[first_key] = "invalid_value_not_expected_type"
        return self._dump(data)


class ExtraTableWithoutPlaceholderScenario(_JsonScenario):
    key = "result_review.extra_table_without_placeholder"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        target, _field = self._choose_field(
            data,
            context,
            lambda field: self._declared_tables(field) == 0,
        )
        if not target:
            return payload
        data[target] = [
            {
                "异常列A": "template has no table placeholder",
                "异常列B": "unexpected table data",
            }
        ]
        return self._dump(data)


class MultipleTablesForSinglePlaceholderScenario(_JsonScenario):
    key = "result_review.multiple_tables_for_single_placeholder"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        target, field = self._choose_field(
            data,
            context,
            lambda candidate: self._declared_tables(candidate) == 1,
        )
        if not target:
            return payload
        headers = self._headers_for(field or {})
        data[target] = [
            [{header: "first table" for header in headers}],
            [{header: "second table" for header in headers}],
        ]
        return self._dump(data)


class MissingTableForPlaceholderScenario(_JsonScenario):
    key = "result_review.missing_table_for_placeholder"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        target, _field = self._choose_field(
            data,
            context,
            lambda field: self._declared_tables(field) > 0,
        )
        if not target:
            return payload
        data[target] = "TEST_FAULT_missing_table_data"
        return self._dump(data)


class TableRowNotObjectScenario(_JsonScenario):
    key = "result_review.table_row_not_object"

    def apply(self, payload: str, context: dict[str, Any]) -> str:
        data = self._load(payload)
        target, _field = self._choose_field(
            data,
            context,
            lambda field: self._declared_tables(field) > 0,
        )
        if not target:
            return payload
        data[target] = [["TEST_FAULT_row_is_not_object"]]
        return self._dump(data)
