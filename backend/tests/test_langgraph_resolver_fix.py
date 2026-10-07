"""LangGraph Runtime Reliability — targeted tests for FIX-A (resolver contract).

Covers:
- TEST-RESOLVER-01: resolve_task_resolver returns resolver (awaited), not coroutine
- TEST-RESOLVER-03: no un-awaited coroutine leak in production call site
- TEST-RESOLVER-05: frozen manifest semantics unchanged
- TEST-RESOLVER-06: static scan — all resolve_task_resolver calls awaited
"""

from __future__ import annotations

import asyncio
import inspect

from app.context_engine.freeze.runtime import (
    build_backfill_resolver,
    resolve_task_resolver,
)

# ── TEST-RESOLVER-01 ───────────────────────────────────────────────
class TestResolverContract:
    """resolve_task_resolver must be awaited; result is a resolver object."""

    def test_returns_coroutine(self):
        """Calling without await returns a coroutine (proving async contract)."""
        coro = resolve_task_resolver("langgraph", {})
        assert inspect.iscoroutine(coro)

    async def test_awaited_returns_resolver(self):
        """Awaiting returns TaskScopedFeatureFlagResolver (not coroutine)."""
        resolver = await resolve_task_resolver("langgraph", {})
        # Must be a resolver with evaluate()
        assert hasattr(resolver, "evaluate")
        val = resolver.evaluate("MIG_NARRATIVE")
        assert isinstance(val, bool)
        # Not a coroutine object
        assert not inspect.iscoroutine(val)

    async def test_frozen_manifest_full_flags(self):
        """Full 23-flag manifest: resolver evaluate returns frozen value."""
        from app.context_engine.freeze.runtime import (
            LANGGRAPH_TASK_SEMANTIC_FLAGS,
            build_resolver_from_manifest,
        )
        from app.context_engine.freeze.service import build_manifest

        flags = dict(LANGGRAPH_TASK_SEMANTIC_FLAGS)
        flags["MIG_NARRATIVE"] = False  # override one flag to verify frozen
        manifest = build_manifest(
            engine="langgraph",
            decision_reason="test",
            canary_bucket=None,
            workspace_key=None,
            context_engine_version="v3",
            task_semantic_flags=flags,
        )
        resolver = build_resolver_from_manifest(manifest)
        assert resolver.evaluate("MIG_NARRATIVE") is False

    async def test_backfill_resolver(self):
        """Legacy backfill resolver returns False for MIG flags (frozen, not env)."""
        resolver = build_backfill_resolver("legacy")
        assert resolver.evaluate("MIG_NARRATIVE") is False


# ── TEST-RESOLVER-06 ───────────────────────────────────────────────
class TestResolverCallSites:
    """Static scan: production call site must await resolve_task_resolver."""

    def test_production_factory_awaits(self):
        """production_runtime_context_factory must await resolve_task_resolver."""
        src = inspect.getsource(
            __import__(
                "app.agent_runtime.production_runtime_context_factory",
                fromlist=["ProductionRuntimeContextFactory"],
            )
        )
        assert "await resolve_task_resolver" in src, (
            "production_runtime_context_factory must await resolve_task_resolver"
        )

    def test_runtime_definition_is_async(self):
        """resolve_task_resolver is declared async (contract)."""
        src = inspect.getsource(resolve_task_resolver)
        assert "async def resolve_task_resolver" in src

    def test_no_unawaited_pattern(self):
        """No bare `return resolve_task_resolver(...)` without await in app/."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "app"
        offenders = []
        for py in root.rglob("*.py"):
            if "production_runtime_context_factory" in str(py):
                continue  # fixed
            try:
                text = py.read_text(encoding="utf-8")
            except Exception:
                continue
            if "resolve_task_resolver(" in text and "await resolve_task_resolver" not in text:
                # bare call without await in same file
                if "def resolve_task_resolver" not in text:
                    offenders.append(str(py))
        assert offenders == [], f"Un-awaited resolve_task_resolver calls: {offenders}"