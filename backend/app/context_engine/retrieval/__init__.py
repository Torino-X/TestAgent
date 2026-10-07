"""CE-03 Retrieval：QueryBuilder / RetrievalExecutor / 审计。

- retrieval.py：QueryBuilder / Weighted RRF / MySQL 权威复核
- executor.py：RetrievalExecutor（Knowledge 双通道）
- audit.py：RetrievalAuditService（Knowledge + Memory 统一审计）
"""

from app.context_engine.retrieval.retrieval import (
    FusionResult,
    QueryBuilder,
    mysql_authoritative_recheck,
    query_hash,
    weighted_rrf,
)
from app.context_engine.retrieval.executor import RetrievalExecutor
from app.context_engine.retrieval.audit import RetrievalAuditService

__all__ = [
    "FusionResult",
    "QueryBuilder",
    "mysql_authoritative_recheck",
    "query_hash",
    "weighted_rrf",
    "RetrievalExecutor",
    "RetrievalAuditService",
]
# auto-appended module-level note: retrieval 子包: 检索阶段入口(retrieve 抽取 + audit)。
