"""ProtectedAnchor 构建与校验（doc09 §11-13）。

- 直接注入型（direct_inject）：Composer 必须从原始来源单独注入，绝不只靠 Summary。
- Summary 必含型（summary_required）：要求压缩输出包含。
- 校验为服务端确定性（不用模型自证）：Source Integrity / Digest Integrity /
  Summary Presence / Semantic Consistency。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.context_engine.compression.models import ProtectedAnchor
from app.context_engine.models.context import ContextRequest, FrozenModel
from app.context_engine.models.selection import SelectedContextSet


def _canonical_digest(value: Any) -> str:
    """canonical JSON 的 SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# 直接注入型：Composer 必须从原始来源单独注入（不依赖 Summary 保留）。
_DIRECT_INJECT_KEYS = {
    "current_user_message",
    "system_rules",
    "call_contract",
    "locked_sections",
    "pending_confirmation",
    "selected_file_ids",
    "source_artifact_id",
    "latest_modification_instruction",
}

# Summary 必含型：要求压缩输出包含（required_in_summary=True）。
_SUMMARY_REQUIRED_KEYS = {
    "task_goal",
    "current_phase",
    "unresolved_decisions",
    "outstanding_items",
    "relevant_failure_reason",
    "loop_state",
}


class ProtectedAnchorBuilder:
    """从 ContextRequest / TaskStateRef / Profile / selected 构建锚点。"""

    def build(
        self,
        request: ContextRequest,
        profile: Any | None = None,
        selected: SelectedContextSet | None = None,
    ) -> tuple[ProtectedAnchor, ...]:
        anchors: list[ProtectedAnchor] = []
        state = request.state_ref or {}

        # ── ContextRequest 来源 ───────────────────────────────────────
        if request.current_user_message:
            anchors.append(
                ProtectedAnchor(
                    key="current_user_message",
                    value_digest=_canonical_digest(request.current_user_message),
                    source_ref="context_request.current_user_message",
                    required_in_summary=False,
                    kind="direct_inject",
                )
            )
        if request.current_node:
            anchors.append(
                ProtectedAnchor(
                    key="current_node",
                    value_digest=_canonical_digest(request.current_node),
                    source_ref="context_request.current_node",
                    required_in_summary=True,
                    kind="summary_required",
                )
            )
        if request.attached_file_ids:
            anchors.append(
                ProtectedAnchor(
                    key="selected_file_ids",
                    value_digest=_canonical_digest(sorted(request.attached_file_ids)),
                    source_ref="context_request.attached_file_ids",
                    required_in_summary=False,
                    kind="direct_inject",
                )
            )

        # ── TaskStateRef（state_ref dict）来源 ────────────────────────
        for key, ref in (
            ("task_goal", "task_state.task_goal"),
            ("task_type", "task_state.task_type"),
            ("pending_confirmation", "task_state.pending_confirmation"),
            ("locked_sections", "task_state.locked_sections"),
            ("source_artifact", "task_state.source_artifact"),
            ("selected_files", "task_state.selected_files"),
        ):
            value = state.get(key)
            if value in (None, "", [], {}):
                continue
            anchors.append(
                ProtectedAnchor(
                    key=key,
                    value_digest=_canonical_digest(value),
                    source_ref=ref,
                    required_in_summary=(key in _SUMMARY_REQUIRED_KEYS),
                    kind="summary_required" if key in _SUMMARY_REQUIRED_KEYS else "direct_inject",
                )
            )

        # ── Profile 来源（system_rules / call_contract）───────────────
        if profile is not None:
            for key in ("system_rules", "call_contract"):
                value = getattr(profile, key, None)
                if value in (None, ""):
                    continue
                anchors.append(
                    ProtectedAnchor(
                        key=key,
                        value_digest=_canonical_digest(value),
                        source_ref=f"profile.{key}",
                        required_in_summary=False,
                        kind="direct_inject",
                    )
                )

        # 去重（同 key 保留首个）
        seen: set[str] = set()
        unique: list[ProtectedAnchor] = []
        for a in anchors:
            if a.key in seen:
                continue
            seen.add(a.key)
            unique.append(a)
        return tuple(unique)


class AnchorValidator:
    """服务端确定性校验（不用模型自证）。"""

    def validate(
        self,
        anchors: tuple[ProtectedAnchor, ...] | list[ProtectedAnchor],
        summary_payload: dict[str, Any] | None = None,
    ) -> tuple[bool, list[str]]:
        """校验锚点完整性。

        summary_payload：压缩输出（结构化 dict），含 anchors 字段或 summary 文本。
        返回 (通过, 失败原因列表)。
        """
        failures: list[str] = []
        anchors = tuple(anchors)
        if not anchors:
            return True, []

        summary_text = ""
        summary_anchors: dict[str, str] = {}
        if summary_payload:
            summary_text = summary_payload.get("summary_text") or summary_payload.get("summary") or ""
            raw_anchors = summary_payload.get("anchors")
            if isinstance(raw_anchors, dict):
                for k, v in raw_anchors.items():
                    summary_anchors[str(k)] = _canonical_digest(v)

        for a in anchors:
            # Digest Integrity：summary 若声明了同 key anchor，digest 必须一致。
            if a.key in summary_anchors:
                if summary_anchors[a.key] != a.value_digest:
                    failures.append(f"anchor_digest_mismatch:{a.key}")
                continue
            # Summary Presence：summary 必含型必须出现在 summary 文本或 anchors。
            if a.required_in_summary:
                # 文本目标校验：确定性关键实体（标题/目标）在 summary 文本中出现。
                if not summary_text or not self._text_contains_anchor(summary_text, a.key):
                    failures.append(f"anchor_missing_from_summary:{a.key}")
            # 直接注入型不要求 summary 包含（Composer 单独注入），但 source 必须存在。
            if not a.source_ref:
                failures.append(f"anchor_missing_source_ref:{a.key}")

        return (len(failures) == 0), failures

    @staticmethod
    def _text_contains_anchor(summary_text: str, key: str) -> bool:
        """确定性文本目标校验：锚点 key 对应概念在 summary 中出现的粗查。

        用于 service 层进一步校验（关键实体在 summary 文本中的出现）。
        """
        # 中文/英文关键概念词表（轻量启发，确定性）。
        _CONCEPT_KEYWORDS = {
            "task_goal": ("目标", "goal"),
            "current_phase": ("阶段", "phase"),
            "unresolved_decisions": ("未决", "待定", "unresolved"),
            "outstanding_items": ("待办", "剩余", "outstanding"),
            "relevant_failure_reason": ("失败原因", "错误", "failure"),
            "loop_state": ("循环", "轮次", "迭代", "loop"),
        }
        keywords = _CONCEPT_KEYWORDS.get(key)
        if not keywords:
            return True
        lowered = summary_text.lower()
        return any(k in lowered for k in keywords)


__all__ = ["ProtectedAnchorBuilder", "AnchorValidator", "_canonical_digest", "_DIRECT_INJECT_KEYS", "_SUMMARY_REQUIRED_KEYS"]
# auto-appended module-level note: anchor 压缩: 关键 message 锚点定位器(避免摘要丢关键需求)。
# auto-appended module-level note: anchor 压缩: 关键 message 锚点定位器(避免摘要丢关键需求)。
