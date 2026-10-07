"""Context Engine v1.0 — 共享上下文基础设施。

角色：共享基础设施，不是 Agent。不决定 LangGraph 下一业务节点，
不修改业务 State。分层遵循上游硬约束：

- ``models``：Pydantic v2 Domain，不依赖 FastAPI / SQLAlchemy / LangGraph
- ``errors``：ContextEngineError（Domain 错误）+ 到 AppError 的转换
- ``feature_flags``：Context Engine Feature Flag
- ``profiles``：ContextProfile 代码注册（非数据库表）
- ``planning``：Planner / Budget Manager（确定性规则，不调用 LLM）
- ``providers``：Provider Protocol / Factory（Chat / Embedding / Reranker）
- ``adapters``：外部依赖注入边界（Provider / TokenEstimator）
"""

from app.context_engine.errors import ContextEngineError, ContextEngineStage
from app.context_engine.feature_flags import ContextEngineFeatureFlags, get_context_engine_flags

__all__ = [
    "ContextEngineError",
    "ContextEngineStage",
    "ContextEngineFeatureFlags",
    "get_context_engine_flags",
]
# auto-appended module-level note: context_engine 子包入口。
