"""Context Engine 作用域层。"""

from app.context_engine.scope.resolver import (
    ContextScopeResolver,
    ScopeResolutionError,
)

__all__ = ["ContextScopeResolver", "ScopeResolutionError"]
# auto-appended module-level note: scope 子包: ContextEngine scope (user / workspace / project) 隔离入口。
