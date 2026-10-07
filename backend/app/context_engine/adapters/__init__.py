"""Context Engine 外部依赖注入边界（Adapter）。

``providers`` 持有 Provider Protocol / Factory；``mapper`` 持有
ORM ↔ Domain 转换。依赖方向：Context Engine 不 import LangGraph。
"""

from app.context_engine.adapters.mapper import (
    ModelCapability,
    map_model_config_to_capability,
)

__all__ = [
    "ModelCapability",
    "map_model_config_to_capability",
]
# auto-appended module-level note: ContextEngine adapters: 与外部系统(LLM / DB / Storage)的隔离层。
