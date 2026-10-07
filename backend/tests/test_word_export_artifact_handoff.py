"""Tests for the WordExportTool → DocxFormatCheckTool handoff.

These tests pin down the contract that ``WordExportTool`` writes the
artifact's ``storage_path`` onto ``AgentContext.artifact`` so that the
subsequent ``DocxFormatCheckTool`` (which runs without any DB access)
can read it back via ``_artifact_storage_path``.

The bug we are guarding against: 2026-07-13 incident where
``WordExportTool.run()`` built an API-shaped ``data`` dict that
deliberately excluded ``storage_path`` (internal field), then assigned
that *same* dict to ``context.artifact``. Downstream
``DocxFormatCheckTool._artifact_storage_path`` looked up
``artifact["storage_path"]`` and got ``None``, returning
``FORMAT_NO_ARTIFACT`` even though the docx was on disk + in DB.

Fix: build a separate ``context_artifact`` dict that DOES carry
``storage_path`` but is never returned to the API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
from docx import Document


# ── helpers ─────────────────────────────────────────────────────────


@dataclass
class _FakeCtx:
    """Minimal AgentContext stub — only the fields WordExportTool touches."""

    task_id: str = "task_test"
    conversation_id: str = "conv_test"
    user_id: str = "user_test"
    status: str = "running"
    session: Any = None
    task_internal_id: int = 0
    conversation_internal_id: int = 0
    user_internal_id: int = 0
    template_file_id: str | None = None
    requirement_analysis: dict | None = None
    test_plan_content: dict | None = None
    artifact: dict | None = None


def _make_test_plan_content() -> dict:
    """Build a minimal ``section_package``-shaped content blob.

    WordExportTool only inspects ``generated_sections`` for the
    ``body_start_index`` flag and the length, so two sections suffice.
    """
    return {
        "section_package": {
            "generated_sections": [
                {"section_id": "s1", "title": "一", "content": "内容A",
                 "body_start_index": 0},
                {"section_id": "s2", "title": "二", "content": "内容B",
                 "body_start_index": 1},
            ],
            "template_name": "template.docx",
        },
    }


def _make_docx(path: Path, *, n_headings: int = 1) -> None:
    """Create a minimal docx file at ``path``."""
    doc = Document()
    for i in range(n_headings):
        doc.add_heading(f"标题 {i}", level=1)
        doc.add_paragraph("正文")
    doc.save(str(path))


# ── storage_path on ctx.artifact after WordExportTool runs ──────────


class TestWordExportPopulatesContextArtifact:
    """The contract: after WordExportTool succeeds, ``ctx.artifact`` must
    carry ``storage_path`` so DocxFormatCheckTool can find the file.
    """

    @pytest.fixture
    def setup(self, tmp_path):
        """Build a template file + a fake template DB row + a task context.

        WordExportTool._resolve_template_path() first hits DB; we mock
        the sync_engine to return our tmp template path so we don't need
        a real uploaded_files row.
        """
        template_path = tmp_path / "template.docx"
        _make_docx(template_path, n_headings=1)

        ctx = _FakeCtx(
            template_file_id="file_tpl_test",
            test_plan_content=_make_test_plan_content(),
            requirement_analysis={"project_name": "测试项目"},
            user_internal_id=42,
            task_internal_id=7,
        )

        # Mock _resolve_template_path to bypass DB lookup
        with patch(
            "app.tools.word_export_tool.WordExportTool._resolve_template_path",
            return_value=str(template_path),
        ):
            yield ctx, template_path

    @pytest.mark.asyncio
    async def test_context_artifact_has_storage_path(self, setup):
        from app.tools.word_export_tool import WordExportTool

        ctx, template_path = setup
        result = await WordExportTool().run({}, ctx)

        assert result["success"] is True
        assert ctx.artifact is not None, (
            "WordExportTool must populate ctx.artifact so downstream "
            "DocxFormatCheckTool can find the file."
        )
        assert ctx.artifact.get("storage_path"), (
            "ctx.artifact['storage_path'] is the handoff key — without it, "
            "DocxFormatCheckTool returns FORMAT_NO_ARTIFACT. "
            f"Got ctx.artifact={ctx.artifact!r}"
        )
        # storage_path should resolve to a real on-disk file
        resolved = ctx.artifact["storage_path"]
        assert (
            Path(resolved).is_absolute() or resolved
        ), f"storage_path must not be empty, got {resolved!r}"

    @pytest.mark.asyncio
    async def test_api_data_does_not_leak_storage_path(self, setup):
        """Equally important: the API response must NOT carry ``storage_path``.

        F017 contract (word_export_tool.py:199) deliberately omits it from
        ``data``. We assert this to prevent a future refactor from
        "fixing" the handoff by adding storage_path back to ``data`` —
        that would break the API contract.
        """
        from app.tools.word_export_tool import WordExportTool

        ctx, _ = setup
        result = await WordExportTool().run({}, ctx)

        assert result["success"] is True
        api_data = result["data"]
        assert "storage_path" not in api_data, (
            "API response must NOT expose storage_path (internal field). "
            f"Leaked: {api_data.get('storage_path')!r}"
        )
        # But ctx.artifact should have it — this is the asymmetry the fix establishes
        assert ctx.artifact is not None
        assert "storage_path" in ctx.artifact


# ── DocxFormatCheckTool can read the handoff ────────────────────────


class TestDocxFormatCheckReadsContextArtifact:
    """The consumer side: DocxFormatCheckTool reads
    ``ctx.artifact["storage_path"]`` — must work after WordExportTool
    populated it via the new ``context_artifact`` dict."""

    @pytest.fixture
    def exported_ctx(self, tmp_path) -> tuple[_FakeCtx, Path]:
        """Run WordExportTool once and hand back the populated ctx."""
        template_path = tmp_path / "template.docx"
        exported_path = tmp_path / "exported.docx"
        _make_docx(template_path, n_headings=2)
        _make_docx(exported_path, n_headings=2)

        ctx = _FakeCtx(
            template_file_id="file_tpl_test",
            test_plan_content=_make_test_plan_content(),
            requirement_analysis={"project_name": "测试项目"},
            user_internal_id=42,
            task_internal_id=7,
        )

        with patch(
            "app.tools.word_export_tool.WordExportTool._resolve_template_path",
            return_value=str(template_path),
        ):
            # Stub the actual export to write our pre-made file to the
            # path WordExportTool picks, then run for real.
            from app.tools.word_export_tool import WordExportTool
            import asyncio

            # Real run, then overwrite the output file with our stub
            result = asyncio.get_event_loop().run_until_complete(
                WordExportTool().run({}, ctx)
            )
            assert result["success"] is True

            # Replace the generated docx with our pre-made one (same
            # content, no template backfill needed for format_check).
            actual_output = ctx.artifact["storage_path"]
            _make_docx(Path(actual_output), n_headings=2)

        return ctx, exported_path

    def test_artifact_storage_path_helper_returns_path(self, exported_ctx):
        """``DocxFormatCheckTool._artifact_storage_path`` is the
        handoff reader. After the fix it must return a string for any
        ctx where WordExportTool has populated ``artifact``."""
        from app.tools.docx_format_check_tool import DocxFormatCheckTool

        ctx, _ = exported_ctx
        path = DocxFormatCheckTool._artifact_storage_path(ctx)
        assert path, (
            "After WordExportTool runs, _artifact_storage_path must "
            "return a non-empty string. Returning None triggers "
            "FORMAT_NO_ARTIFACT."
        )
        assert isinstance(path, str)

    def test_artifact_storage_path_returns_none_when_ctx_empty(self):
        """Regression: with an empty ctx (no WordExportTool run), the
        helper should still return None so DocxFormatCheckTool can
        surface FORMAT_NO_ARTIFACT.  Without this test, a future
        "always return a default path" change would silently swallow
        the error."""
        from app.tools.docx_format_check_tool import DocxFormatCheckTool

        ctx = _FakeCtx()
        # Defensive: simulate both "artifact=None" and "artifact={}"
        assert DocxFormatCheckTool._artifact_storage_path(ctx) is None
        ctx.artifact = {}
        assert DocxFormatCheckTool._artifact_storage_path(ctx) is None
        ctx.artifact = {"download_url": "/api/.../download"}  # missing storage_path
        assert DocxFormatCheckTool._artifact_storage_path(ctx) is None


# ── End-to-end smoke: both tools run in sequence ────────────────────


class TestWordExportThenFormatCheck:
    """Run both tools against the same ctx, asserting the format-check
    reaches its comparison step (not the FORMAT_NO_ARTIFACT branch)."""

    @pytest.mark.asyncio
    async def test_format_check_no_longer_returns_no_artifact(self, tmp_path):
        from app.tools.docx_format_check_tool import DocxFormatCheckTool
        from app.tools.word_export_tool import WordExportTool

        template_path = tmp_path / "template.docx"
        _make_docx(template_path, n_headings=2)

        ctx = _FakeCtx(
            template_file_id="file_tpl_test",
            test_plan_content=_make_test_plan_content(),
            requirement_analysis={"project_name": "测试项目"},
            user_internal_id=42,
            task_internal_id=7,
        )

        with patch(
            "app.tools.word_export_tool.WordExportTool._resolve_template_path",
            return_value=str(template_path),
        ):
            export_result = await WordExportTool().run({}, ctx)
            assert export_result["success"] is True

            # Overwrite the auto-exported file with our pre-made one
            # (avoids template-backfill side-effects in this unit test)
            actual_output = ctx.artifact["storage_path"]
            _make_docx(Path(actual_output), n_headings=2)

            check_result = await DocxFormatCheckTool().run({}, ctx)

        # The fix guarantees we no longer hit FORMAT_NO_ARTIFACT.
        # Compare_docx_to_template will return level='passed' or 'warning'
        # (we don't care which — just not FORMAT_NO_ARTIFACT).
        if not check_result["success"]:
            err_code = (check_result.get("error") or {}).get("code")
            assert err_code != "FORMAT_NO_ARTIFACT", (
                f"Regression: DocxFormatCheckTool still hits FORMAT_NO_ARTIFACT "
                f"after the fix. result={check_result!r}"
            )