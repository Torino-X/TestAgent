"""ContextEngine 组装工厂（DI）。

CE-02 WP-8：全部依赖构造注入，测试可注入 Fake/内存实现。
"""

from __future__ import annotations

from app.context_engine.composer.composer import ContextComposer
from app.context_engine.composer.validator import ComposeValidator
from app.context_engine.compression.preflight_service import ContextPreflightService
from app.context_engine.debug.prompt_dumper import ContextPromptDumper
from app.context_engine.feature_flags import get_context_engine_flags
from app.context_engine.planning.planner import ContextPlanner
from app.context_engine.runtime.context_engine import ContextEngine
from app.context_engine.scope.resolver import ContextScopeResolver
from app.context_engine.selection.dedup import ContextDeduplicator
from app.context_engine.selection.quota import SourceQuotaEnforcer, SourceQuotaPolicy
from app.context_engine.selection.selector import ContextSelector
from app.context_engine.sources.orchestrator import SourceOrchestrator
from app.context_engine.sources.registry import SourceAdapterRegistry


def build_context_engine(
    *,
    source_registry: SourceAdapterRegistry | None = None,
    snapshot_writer=None,
    planner: ContextPlanner | None = None,
    scope_resolver: ContextScopeResolver | None = None,
    selector: ContextSelector | None = None,
    deduplicator: ContextDeduplicator | None = None,
    quota_enforcer: SourceQuotaEnforcer | None = None,
    composer: ContextComposer | None = None,
    validator: ComposeValidator | None = None,
    token_counter=None,
    preflight: ContextPreflightService | None = None,
    prompt_dumper: ContextPromptDumper | None = None,
) -> ContextEngine:
    """组装全部依赖（默认组件 + 可覆盖注入）。"""
    planner = planner or ContextPlanner()
    scope_resolver = scope_resolver or ContextScopeResolver()
    source_registry = source_registry or SourceAdapterRegistry()
    orchestrator = SourceOrchestrator(source_registry, token_counter=token_counter)
    selector = selector or ContextSelector()
    deduplicator = deduplicator or ContextDeduplicator()
    quota_enforcer = quota_enforcer or SourceQuotaEnforcer(SourceQuotaPolicy())
    composer = composer or ContextComposer(token_counter=token_counter)
    validator = validator or ComposeValidator(token_counter=token_counter)
    if preflight is None:
        flags = get_context_engine_flags()
        preflight = ContextPreflightService(
            enabled=bool(
                flags.context_engine_enabled and flags.context_compaction_enabled
            ),
            full_replace_enabled=bool(flags.context_full_replace_enabled),
        )
    if prompt_dumper is None:
        flags = get_context_engine_flags()
        prompt_dumper = ContextPromptDumper(enabled=flags.context_prompt_dump_enabled)
    return ContextEngine(
        planner=planner,
        scope_resolver=scope_resolver,
        source_orchestrator=orchestrator,
        selector=selector,
        deduplicator=deduplicator,
        quota_enforcer=quota_enforcer,
        composer=composer,
        validator=validator,
        snapshot_writer=snapshot_writer,
        token_counter=token_counter,
        preflight=preflight,
        prompt_dumper=prompt_dumper,
    )
# auto-appended module-level note: engine factory: 按 build_context 入口协议产出 ContextEngine 实例。
