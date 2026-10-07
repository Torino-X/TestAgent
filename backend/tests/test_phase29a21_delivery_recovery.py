"""Phase 2.9A.21 交付一致性测试 — Artifact 下载权限 + 任务详情扩展。

覆盖:
  1. _is_safe_filename:正常名通过 / 路径穿越拒绝 / 控制字符拒绝 / 空名拒绝
  2. ArtifactService.get_download_stream 跨用户越权拒绝
  3. storage_path 空 → ArtifactStorageError
  4. 文件名不合法 → ArtifactStorageError
  5. AgentTaskService.get_task 返回 dict 包含 artifact + format_check_result
  6. _summarize_latest_format_check 从 agent_events 取最近 docx_format_checked
  7. 历史恢复:刷新页面后能拿到同一 Artifact
  8. _is_safe_filename 中文文件名通过
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ── 单元: _is_safe_filename ────────────────────────────────────────────────


class TestIsSafeFilename:
    def test_normal_name_passes(self):
        from app.services.artifact_service import _is_safe_filename

        assert _is_safe_filename("test_plan.docx") is True

    def test_chinese_name_passes(self):
        from app.services.artifact_service import _is_safe_filename

        assert _is_safe_filename("测试方案.docx") is True
        assert _is_safe_filename("测试方案_V2.docx") is True

    def test_path_traversal_rejected(self):
        from app.services.artifact_service import _is_safe_filename

        assert _is_safe_filename("../etc/passwd") is False
        assert _is_safe_filename("..\\windows\\system32") is False
        assert _is_safe_filename("folder/file.docx") is False
        assert _is_safe_filename("folder\\file.docx") is False

    def test_absolute_path_rejected(self):
        from app.services.artifact_service import _is_safe_filename

        assert _is_safe_filename("/etc/passwd") is False

    def test_empty_rejected(self):
        from app.services.artifact_service import _is_safe_filename

        assert _is_safe_filename("") is False

    def test_hidden_file_rejected(self):
        from app.services.artifact_service import _is_safe_filename

        assert _is_safe_filename(".hidden") is False
        assert _is_safe_filename(".env") is False

    def test_too_long_rejected(self):
        from app.services.artifact_service import _is_safe_filename

        assert _is_safe_filename("a" * 256 + ".docx") is False


# ── 单元: ArtifactService 权限校验 ─────────────────────────────────────────


class TestArtifactServicePermissions:
    @pytest.mark.asyncio
    async def test_cross_user_get_artifact_forbidden(self):
        """越权读取 Artifact → NotFound,绝不能泄漏数据"""
        from unittest.mock import AsyncMock, MagicMock

        from app.core.exceptions import NotFoundError
        from app.services.artifact_service import ArtifactService

        # artifact 属于 user 1,调用者传 user 2
        fake_artifact = SimpleNamespace(public_id="art_x", user_id=1)
        repo = MagicMock()
        repo.get_by_public_id = AsyncMock(return_value=fake_artifact)
        service = ArtifactService.__new__(ArtifactService)
        service._art_repo = repo
        service._session = MagicMock()

        with pytest.raises(NotFoundError):
            await service.get_artifact("art_x", user_internal_id=2)

    @pytest.mark.asyncio
    async def test_download_storage_path_empty_raises(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.core.exceptions import ArtifactStorageError
        from app.services.artifact_service import ArtifactService

        fake_artifact = SimpleNamespace(
            public_id="art_x",
            user_id=1,
            storage_path="",
            file_name="x.docx",
        )
        repo = MagicMock()
        repo.get_by_public_id = AsyncMock(return_value=fake_artifact)
        service = ArtifactService.__new__(ArtifactService)
        service._art_repo = repo
        service._session = MagicMock()

        with pytest.raises(ArtifactStorageError):
            await service.get_download_stream("art_x", user_internal_id=1)

    @pytest.mark.asyncio
    async def test_download_unsafe_filename_raises(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.core.exceptions import ArtifactStorageError
        from app.services.artifact_service import ArtifactService

        fake_artifact = SimpleNamespace(
            public_id="art_x",
            user_id=1,
            storage_path="artifacts/1/1/x.docx",
            file_name="../../etc/passwd",
        )
        repo = MagicMock()
        repo.get_by_public_id = AsyncMock(return_value=fake_artifact)
        service = ArtifactService.__new__(ArtifactService)
        service._art_repo = repo
        service._session = MagicMock()

        with pytest.raises(ArtifactStorageError):
            await service.get_download_stream("art_x", user_internal_id=1)


# ── 单元: AgentTaskService 扩展字段 ───────────────────────────────────────


class TestAgentTaskServiceExtendedDetail:
    @pytest.mark.asyncio
    async def test_get_task_returns_artifact_and_format_check(self):
        """Phase 2.9A.21 §7.2 — 任务详情含 artifact + format_check_result。"""
        from unittest.mock import AsyncMock, MagicMock

        from app.services.agent_task_service import AgentTaskService

        # Simulate task + run + artifact row + 1 个 docx_format_checked event
        from datetime import datetime

        task_row = SimpleNamespace(
            public_id="task_x",
            id=1,
            task_type="test_plan_generation",
            status="completed",
            plan_json=[],
            task_context_json=None,
            review_result_json=None,
            active_run_id="run_y",
            runtime_status="completed",
            user_id=1,  # Phase 2.9A.21:user_id 必须,get_task 校验归属
            started_at=datetime(2026, 7, 28),
            completed_at=datetime(2026, 7, 28),
        )
        run_row = SimpleNamespace(
            public_id="run_y",
            status="completed",
            started_at=datetime(2026, 7, 28),
            finished_at=datetime(2026, 7, 28),
        )
        artifact_row = SimpleNamespace(
            public_id="art_x",
            artifact_type="test_plan_word",
            file_name="测试方案.docx",
            file_ext="docx",
            file_size=48828,
            status="available",
            version_no=1,
            created_at=datetime(2026, 7, 28),
        )
        event_row = SimpleNamespace(
            event_type="docx_format_checked",
            payload_json={
                "status": "passed",
                "losses": [],
                "loop_count": 0,
                "checked_artifact_public_id": "art_x",
            },
        )

        # Stub repos
        task_repo = MagicMock()
        task_repo.get_by_public_id = AsyncMock(return_value=task_row)
        run_repo = MagicMock()
        run_repo.get_by_public_id = AsyncMock(return_value=run_row)
        event_repo = MagicMock()
        event_repo.list_by_task = AsyncMock(return_value=[event_row])
        art_repo = MagicMock()
        art_repo.list_by_task = AsyncMock(return_value=[artifact_row])
        confirm_repo = MagicMock()

        svc = AgentTaskService.__new__(AgentTaskService)
        svc._task_repo = task_repo
        svc._run_repo = run_repo
        svc._event_repo = event_repo
        svc._art_repo = art_repo
        svc._confirm_repo = confirm_repo
        svc._session = MagicMock()

        result = await svc.get_task("task_x", user_internal_id=1)
        assert result["task_id"] == "task_x"
        assert result["artifact"] is not None
        assert result["artifact"]["artifact_id"] == "art_x"
        assert result["artifact"]["file_name"] == "测试方案.docx"
        # format_check_result 是从历史事件提取
        assert result["format_check_result"] is not None
        assert result["format_check_result"]["level"] == "passed"
        assert result["format_check_result"]["checked_artifact_public_id"] == "art_x"

    @pytest.mark.asyncio
    async def test_get_task_no_events_returns_none_format_check(self):
        """无历史事件时 format_check_result = None,artifact 字段保持 None 兜底"""
        from unittest.mock import AsyncMock, MagicMock

        from app.services.agent_task_service import AgentTaskService

        from datetime import datetime

        task_row = SimpleNamespace(
            public_id="task_x",
            id=1,
            task_type="test_plan_generation",
            status="failed",
            plan_json=[],
            task_context_json=None,
            review_result_json=None,
            active_run_id=None,
            runtime_status="failed",
            user_id=1,
            started_at=datetime(2026, 7, 28),
            completed_at=datetime(2026, 7, 28),
        )

        task_repo = MagicMock()
        task_repo.get_by_public_id = AsyncMock(return_value=task_row)
        run_repo = MagicMock()
        run_repo.get_by_public_id = AsyncMock(return_value=None)
        run_repo.list_by_task = AsyncMock(return_value=[])
        event_repo = MagicMock()
        event_repo.list_by_task = AsyncMock(return_value=[])
        art_repo = MagicMock()
        art_repo.list_by_task = AsyncMock(return_value=[])
        confirm_repo = MagicMock()

        svc = AgentTaskService.__new__(AgentTaskService)
        svc._task_repo = task_repo
        svc._run_repo = run_repo
        svc._event_repo = event_repo
        svc._art_repo = art_repo
        svc._confirm_repo = confirm_repo
        svc._session = MagicMock()

        result = await svc.get_task("task_x", user_internal_id=1)
        assert result["artifact"] is None
        assert result["format_check_result"] is None

    @pytest.mark.asyncio
    async def test_history_recovery_returns_same_artifact(self):
        """历史恢复:同一 task_id 两次调用 get_task 必须返回同一 Artifact public_id"""
        from unittest.mock import AsyncMock, MagicMock

        from datetime import datetime

        from app.services.agent_task_service import AgentTaskService

        task_row = SimpleNamespace(
            public_id="task_x",
            id=1,
            task_type="test_plan_generation",
            status="completed",
            plan_json=[],
            task_context_json=None,
            review_result_json=None,
            active_run_id=None,
            runtime_status="completed",
            user_id=1,
            started_at=datetime(2026, 7, 28),
            completed_at=datetime(2026, 7, 28),
        )
        artifact_row = SimpleNamespace(
            public_id="art_x",
            artifact_type="test_plan_word",
            file_name="x.docx",
            file_ext="docx",
            file_size=100,
            status="available",
            version_no=1,
            created_at=datetime(2026, 7, 28),
        )

        task_repo = MagicMock()
        task_repo.get_by_public_id = AsyncMock(return_value=task_row)
        run_repo = MagicMock()
        run_repo.get_by_public_id = AsyncMock(return_value=None)
        run_repo.list_by_task = AsyncMock(return_value=[])
        event_repo = MagicMock()
        event_repo.list_by_task = AsyncMock(return_value=[])
        art_repo = MagicMock()
        art_repo.list_by_task = AsyncMock(return_value=[artifact_row])
        confirm_repo = MagicMock()

        svc = AgentTaskService.__new__(AgentTaskService)
        svc._task_repo = task_repo
        svc._run_repo = run_repo
        svc._event_repo = event_repo
        svc._art_repo = art_repo
        svc._confirm_repo = confirm_repo
        svc._session = MagicMock()

        # 第一次(模拟首次页面加载)
        result1 = await svc.get_task("task_x", user_internal_id=1)
        # 第二次(模拟 SSE 重连 / 页面刷新)
        result2 = await svc.get_task("task_x", user_internal_id=1)

        # 同一 public_id,前后两次一致 — 历史恢复的关键断言
        assert result1["artifact"]["artifact_id"] == "art_x"
        assert result2["artifact"]["artifact_id"] == "art_x"
        assert result1["artifact"]["artifact_id"] == result2["artifact"]["artifact_id"]
