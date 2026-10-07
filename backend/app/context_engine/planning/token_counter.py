"""TokenCounter — 统一的 token 估算服务（整改 v2 + WP-BE-04）。

启发式只能作为 fallback（中文 ~1/1.5 chars）。未知模型使用保守 multiplier，
通过 ModelCapability / Planner 结果可审计。

WP-BE-04 状态语义修正：**只有真实 tokenizer 被调用才允许 count_mode='exact' /
estimated=false**。当前没有任何真实 tokenizer（仅 tokenizer_name 元数据 +
字符估算），因此即使 tokenizer_name 存在也一律标记 heuristic + estimated=true，
禁止伪装精确值（守禁令：不引入大型 Transformers 权重）。

结果携带：
- count_mode: exact | heuristic | unknown
- estimated: 是否为估算值（exact=false；heuristic/unknown=true）
- tokenizer_name: 使用的 tokenizer（或 heuristic）
- fallback_reason: 为什么降级到启发式
- safety_multiplier: 未知模型的安全系数（≥1）
- estimated_tokens: 最终估算值
"""

from __future__ import annotations

from dataclasses import dataclass

from app.common.token_estimator import estimate_tokens as _heuristic_estimate

HEURISTIC_TOKENIZER = "heuristic"

# 未知模型/未知窗口的保守安全系数（守禁令：Tokenizer 不可用时提高 Safety Margin）
_UNKNOWN_MODEL_MULTIPLIER = 1.5
_KNOWN_MODEL_MULTIPLIER = 1.0


@dataclass(frozen=True)
class TokenCountResult:
    """token 估算结果（含降级原因，可审计）。"""

    estimated_tokens: int
    count_mode: str  # exact | heuristic | unknown
    tokenizer_name: str
    fallback_reason: str | None = None
    safety_multiplier: float = 1.0
    estimated: bool = True

    @property
    def tokens(self) -> int:
        """兼容旧字段名。"""
        return self.estimated_tokens

    @property
    def uses_heuristic(self) -> bool:
        return self.count_mode in ("heuristic", "unknown")

    def model_dump(self) -> dict[str, object]:
        """序列化（API / 审计使用）。"""
        return {
            "estimated_tokens": self.estimated_tokens,
            "count_mode": self.count_mode,
            "tokenizer_name": self.tokenizer_name,
            "fallback_reason": self.fallback_reason,
            "safety_multiplier": self.safety_multiplier,
            "estimated": self.estimated,
        }


class TokenCounter:
    """估算文本 token 数。

    WP-BE-04：``tokenizer_name`` 只是元数据；本实现不携带真实 tokenizer，
    因此**永不标注 exact**（无真实 tokenizer 被调用）。真实 tokenizer 接入后
    可通过 ``estimate_exact`` 显式返回 exact。

    - 无真实 tokenizer → heuristic（estimated=true）；
    - 模型窗口/能力未知 → unknown（更高 safety_multiplier）。
    """

    def __init__(
        self,
        tokenizer_name: str | None = None,
        *,
        model_known: bool = True,
    ) -> None:
        self._tokenizer_name = tokenizer_name or HEURISTIC_TOKENIZER
        self._model_known = model_known

    def estimate(self, text: str | None) -> TokenCountResult:
        if not text:
            return TokenCountResult(
                estimated_tokens=0,
                count_mode="exact",
                tokenizer_name=self._tokenizer_name,
                estimated=False,
            )

        raw = _heuristic_estimate(text)

        if not self._model_known:
            # 未知模型 → 保守 multiplier（仍 heuristic，不得冒充 exact）
            return TokenCountResult(
                estimated_tokens=int(raw * _UNKNOWN_MODEL_MULTIPLIER),
                count_mode="unknown",
                tokenizer_name=HEURISTIC_TOKENIZER,
                fallback_reason="model_unknown",
                safety_multiplier=_UNKNOWN_MODEL_MULTIPLIER,
                estimated=True,
            )

        # 启发式 fallback（即使 tokenizer_name 存在——未真实调用 tokenizer）
        return TokenCountResult(
            estimated_tokens=raw,
            count_mode="heuristic",
            tokenizer_name=HEURISTIC_TOKENIZER,
            fallback_reason="no_real_tokenizer",
            safety_multiplier=_KNOWN_MODEL_MULTIPLIER,
            estimated=True,
        )

    def estimate_exact(self, text: str | None, count: int) -> TokenCountResult:
        """显式精确计数（仅当真实 tokenizer 已被调用时使用）。

        WP-BE-04：只有真实 tokenizer 的输出才能走此方法 → count_mode='exact' /
        estimated=false。
        """
        return TokenCountResult(
            estimated_tokens=max(0, int(count or 0)),
            count_mode="exact",
            tokenizer_name=self._tokenizer_name,
            safety_multiplier=_KNOWN_MODEL_MULTIPLIER,
            estimated=False,
        )

    @property
    def uses_heuristic(self) -> bool:
        return self._tokenizer_name == HEURISTIC_TOKENIZER


_default_token_counter = TokenCounter()


def count_tokens(text: str | None) -> int:
    """便捷函数：返回 token 数（默认启发式 tokenizer）。"""
    return _default_token_counter.estimate(text).tokens
# auto-appended module-level note: token 计数器: 估算 text/JSON/列表 长度, 给 budget_calculator 调用。
