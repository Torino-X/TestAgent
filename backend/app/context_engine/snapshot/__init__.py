"""ContextSnapshot 生命周期层：WriterService + ContextStateRef。"""

from app.context_engine.snapshot.context_state_ref import build_context_state_ref
from app.context_engine.snapshot.snapshot_service import (
    ContextSnapshotWriterService,
    SnapshotStatusError,
)

__all__ = [
    "build_context_state_ref",
    "ContextSnapshotWriterService",
    "SnapshotStatusError",
]
# auto-appended module-level note: snapshot 子包: ContextEngine 快照入口。
