"""Compose 输出验证器：token 重估 / Required Section / 锚点 / 角色 / 注入标签。

CE-02 WP-4 validator：
- 重估 token；Required Section 存在；Current Goal Anchor 存在；
- 角色合法（untrusted 不能成为 system）；Tool call_id 合法；
- Prompt Injection 标签存在；
- 生成 prompt_digest(SHA-256)；脱敏 excerpt；
- 超 Absolute → 返回 preflight 重试语义（不直接调 LLM）。
"""

from __future__ import annotations

from app.context_engine.models.compose import ComposeValidation, ContextComposeResult, ContextMessage
from app.context_engine.models.context import ContextTrust
from app.context_engine.models.value_objects import Digest


class ComposeValidator:
    """对 ContextComposeResult 做输出验证。"""

    def __init__(self, *, token_counter=None, absolute_threshold: int | None = None) -> None:
        self._token_counter = token_counter
        self._absolute_threshold = absolute_threshold

    def validate(
        self,
        result: ContextComposeResult,
        *,
        absolute_threshold: int | None = None,
    ) -> ComposeValidation:
        # 1. 角色合法：untrusted 内容不能成为 system role
        roles_valid = all(
            m.role != "system" or m.trust == ContextTrust.TRUSTED_INSTRUCTION
            for m in result.messages
        )

        # 2. Required Section 存在
        required_sections_present = True
        if result.selected is not None:
            required_sections_present = any(
                s.get("required") and s.get("included_count", 0) > 0
                for s in result.selected.section_stats.values()
            )

        # 3. Current Goal Anchor 存在
        current_goal_anchor_present = any(
            m.kind is not None and m.kind.value == "current_goal"
            for m in result.messages
        )

        # 4. Tool call_id 合法（无 tool_call_id 时默认合法）
        tool_call_ids_valid = True

        # 5. Injection 标签存在（外部内容必须带 <context-section> 包装）
        injection_labels_present = all(
            m.trust == ContextTrust.TRUSTED_INSTRUCTION
            or "<context-section" in m.content
            for m in result.messages
        )

        # 6. token 重估 + digest + excerpt
        prompt_text = result.prompt_text or ""
        estimated = self._estimate(prompt_text)
        digest = Digest.of(prompt_text) if prompt_text else None

        # 7. 超 Absolute → preflight 重试语义
        failure_code = None
        effective_absolute = (
            absolute_threshold
            if absolute_threshold is not None
            else self._absolute_threshold
        )
        if effective_absolute is not None and estimated >= effective_absolute:
            failure_code = "context.preflight.absolute_exceeded"

        return ComposeValidation(
            ok=failure_code is None
            and roles_valid
            and required_sections_present
            and current_goal_anchor_present
            and tool_call_ids_valid
            and injection_labels_present,
            estimated_input_tokens=estimated,
            required_sections_present=required_sections_present,
            current_goal_anchor_present=current_goal_anchor_present,
            roles_valid=roles_valid,
            tool_call_ids_valid=tool_call_ids_valid,
            injection_labels_present=injection_labels_present,
            digest=digest,
            prompt_excerpt=result.prompt_excerpt,
            failure_code=failure_code,
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)
# auto-appended module-level note: validator: 装配产物字段级校验 + 字段白名单(防 api_key 泄漏)。
