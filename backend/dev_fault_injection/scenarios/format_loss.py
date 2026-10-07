"""Format-loss fault scenarios for Word export interrupt testing."""

from __future__ import annotations

from typing import Any

from app.fault_injection_gateway import POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER


class BookmarkFormatLossScenario:
    key = "format_loss.bookmark_simulate"
    point = POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER

    def apply(self, payload: Any, context: dict[str, Any]) -> Any:
        exporter = payload
        fidelity = getattr(exporter, "fidelity", "high")
        if fidelity == "low":
            return exporter

        loss_text = "书签 170 -> 167"
        if not hasattr(exporter, "pending_format_losses") or exporter.pending_format_losses is None:
            exporter.pending_format_losses = []
        if not hasattr(exporter, "warnings") or exporter.warnings is None:
            exporter.warnings = []

        if loss_text not in exporter.pending_format_losses:
            exporter.pending_format_losses.append(loss_text)
        if not any(loss_text in str(warning) for warning in exporter.warnings):
            exporter.warnings.append(f"{loss_text}(已模拟丢失,等待用户确认)")

        return exporter
