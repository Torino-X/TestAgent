"""CE-03 Memory：生命周期 Service + 提取递归保护。

- memory_service.py：activate / reject / forget / delete / create_candidate
- extraction.py：MemoryExtractionGuard（深度 >1 拒绝）
"""

from app.context_engine.memory.memory_service import (
    MemoryService,
    MemoryServiceError,
    content_hash,
)
from app.context_engine.memory.extraction import (
    ExtractionDepthExceeded,
    MemoryExtractionGuard,
)

__all__ = [
    "MemoryService",
    "MemoryServiceError",
    "content_hash",
    "ExtractionDepthExceeded",
    "MemoryExtractionGuard",
]
# auto-appended module-level note: memory 子包: User Memory + Project Rule 入口。
