"""Context Engine 检索 Fake 实现（依赖注入替换，不进生产业务判断）。

设计文档 14 号报告 / 五阶段提示词 §6：
- Fake 必须实现与生产 Adapter 相同的内部 Protocol；
- 通过依赖注入替换，禁止在生产逻辑中硬编码测试结果；
- 返回稳定可重复结果，支持故障注入 / 空结果 / 超时场景。
"""

from __future__ import annotations

from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineFailure,
    ContextEngineStage,
    raise_engine_error,
)
from app.context_engine.providers.protocols import (
    RerankItem,
    RerankRequest,
    RerankResult,
    RerankScore,
    RerankServiceProtocol,
)


class FakeReranker(RerankServiceProtocol):
    """Fake 重排：稳定可重复、按原文顺序返回确定性分数。

    禁止用于生产业务判断；仅用于测试与离线评估。
    """

    def __init__(self, *, enabled: bool = True, fail_requested: bool = False) -> None:
        self._enabled = enabled
        self._fail_requested = fail_requested

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def rerank(self, request: RerankRequest) -> RerankResult:
        if not self._enabled:
            raise_engine_error(
                code="context.rerank.disabled",
                detail="Reranker 已禁用，使用 Weighted RRF 降级",
                stage=ContextEngineStage.RERANK,
                retryable=False,
                recoverable=True,
            )
        if self._fail_requested:
            raise_engine_error(
                code="context.rerank.fake_failure",
                detail="FakeReranker 故障注入",
                stage=ContextEngineStage.RERANK,
                retryable=True,
                recoverable=True,
            )
        # 确定性降序分数（index 越大分数越低），稳定可重复
        items = list(request.items)
        scores = [
            RerankScore(
                doc_id=item.doc_id,
                score=round(1.0 - (i * 0.01), 4),
            )
            for i, item in enumerate(items)
        ]
        scores.sort(key=lambda s: s.score, reverse=True)
        return RerankResult(
            request_id=request.request_id,
            model=request.model,
            scores=scores,
            latency_ms=1,
        )


class DisabledReranker(RerankServiceProtocol):
    """显式 Disabled 的 Reranker：任何调用都抛 disabled 错误。

    用于"尚未确定真实 Provider"时（五阶段提示词 §10 选项 B），
    当前功能显式 Disabled，后续由具体 Provider Adapter 实现。
    """

    def __init__(self, reason: str = "未配置 Reranker Provider") -> None:
        self._reason = reason

    @property
    def enabled(self) -> bool:
        return False

    async def rerank(self, request: RerankRequest) -> RerankResult:
        raise_engine_error(
            code="context.rerank.disabled",
            detail=self._reason,
            stage=ContextEngineStage.RERANK,
            retryable=False,
            recoverable=True,
        )
# auto-appended module-level note: 测试 fake provider: 单测时不需要调真实 LLM。
