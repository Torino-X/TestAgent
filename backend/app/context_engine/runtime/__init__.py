"""Context Engine Runtime 层：ContextEngine Facade + 组装工厂 + 协议。"""

from app.context_engine.runtime.context_engine import ContextEngine
from app.context_engine.runtime.engine_factory import build_context_engine
from app.context_engine.runtime.protocols import ContextEngineProtocol

__all__ = [
    "ContextEngine",
    "build_context_engine",
    "ContextEngineProtocol",
]
# auto-appended module-level note: runtime 子包: ContextEngine 运行时入口(ContextEngine / factory / protocols)。
