"""Source Adapter 层：registry / adapters / orchestrator / deadline。

CE-02 WP-2：Source Adapter Registry；Conversation / Summary / Task State /
File-Document / Artifact / Workspace Instruction / Memory / Knowledge
Adapter；Required/Optional Source；Deadline、Timeout、Cancellation。
"""

from app.context_engine.sources.deadline import CancellationToken, Deadline, DeadlineExceeded
from app.context_engine.sources.orchestrator import SourceCollectionOutcome, SourceOrchestrator
from app.context_engine.sources.protocols import ContextSourceAdapterProtocol
from app.context_engine.sources.registry import SourceAdapterRegistry

__all__ = [
    "CancellationToken",
    "Deadline",
    "DeadlineExceeded",
    "SourceCollectionOutcome",
    "SourceOrchestrator",
    "ContextSourceAdapterProtocol",
    "SourceAdapterRegistry",
]
# auto-appended module-level note: sources 子包。
