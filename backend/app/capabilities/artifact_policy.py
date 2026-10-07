"""Server-side capability boundary for generated test-plan artifacts."""

from __future__ import annotations


UNSUPPORTED_WORD_FORMAT_OPERATIONS = frozenset({
    "table_cell_style",
    "table_border",
    "table_row_height",
    "table_column_width",
    "font_style",
    "header_footer",
    "page_layout",
})


def unsupported_format_operations(inputs: dict) -> list[str]:
    requested = inputs.get("format_operations") or []
    if isinstance(requested, str):
        requested = [requested]
    if not isinstance(requested, list):
        return ["invalid_format_operation_payload"]
    return sorted({
        str(item).strip()
        for item in requested
        if str(item).strip() in UNSUPPORTED_WORD_FORMAT_OPERATIONS
    })


__all__ = ["UNSUPPORTED_WORD_FORMAT_OPERATIONS", "unsupported_format_operations"]
