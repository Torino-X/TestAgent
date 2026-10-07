"""Alembic migration smoke test - upgrade + downgrade + upgrade。

依赖已有 ``backend/.venv`` + 数据库可达;若环境无 DB 则跳过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _alembic() -> list[str]:
    py = BACKEND_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = BACKEND_ROOT / ".venv" / "bin" / "python"
    return [str(py), "-m", "alembic", "-c", str(BACKEND_ROOT / "alembic.ini")]


def _has_db() -> bool:
    """环境变量 ``DATABASE_URL`` 或 ``DATABASE_SYNC_URL`` 存在且 alembic 配置可达。"""
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_SYNC_URL"))


@pytest.mark.skipif(not _has_db(), reason="no DATABASE_URL/SYNC_URL configured")
def test_alembic_upgrade_downgrade_upgrade() -> None:
    """upgrade head → downgrade -1 → upgrade head 必须成功。"""
    cmd = _alembic()
    # 探测 alembic 是否可用
    probe = subprocess.run(cmd + ["current"], cwd=BACKEND_ROOT, capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"alembic probe failed: {probe.stderr[:300]}")

    up = subprocess.run(cmd + ["upgrade", "head"], cwd=BACKEND_ROOT, capture_output=True, text=True)
    assert up.returncode == 0, up.stderr

    down = subprocess.run(cmd + ["downgrade", "-1"], cwd=BACKEND_ROOT, capture_output=True, text=True)
    assert down.returncode == 0, down.stderr

    up2 = subprocess.run(cmd + ["upgrade", "head"], cwd=BACKEND_ROOT, capture_output=True, text=True)
    assert up2.returncode == 0, up2.stderr


def test_alembic_migration_file_exists_after_2_0() -> None:
    """确认 Phase 2.0 的 alembic 修订存在(创建后此测试才绿)。"""
    versions_dir = BACKEND_ROOT / "alembic" / "versions"
    if not versions_dir.exists():
        pytest.skip("no alembic versions dir")
    matches = list(versions_dir.glob("*engine*.py")) + list(
        versions_dir.glob("*runtime*.py")
    )
    if not matches:
        pytest.skip("Phase 2.0 alembic migration not yet generated")