"""CE-01 整改：官方数据库初始化（Baseline Bootstrap）端到端测试。

验证：
- Fresh Empty Database → Official Initialization → Alembic Head → Schema Verification
- 非空库拒绝
- 不触碰已有业务库
"""

from __future__ import annotations

import os

import pytest

_TEST_DB = os.environ.get("DATABASE_SYNC_URL", "")
REQUIRES_TEST_MYSQL = pytest.mark.skipif(
    not _TEST_DB or "testagent" not in _TEST_DB,
    reason="需要 DATABASE_SYNC_URL 指向测试 MySQL",
)

import sqlalchemy as sa

from app.services.db_bootstrap import _is_truly_empty, _precreate_alembic_version


def _sync_url():
    return _TEST_DB.replace("mysql+aiomysql://", "mysql+pymysql://", 1)


def _db_url(db_name: str) -> str:
    """构造指向指定数据库的 sync URL。"""
    return _sync_url().rsplit("/", 1)[0] + f"/{db_name}?charset=utf8mb4"


@REQUIRES_TEST_MYSQL
def test_detects_truly_empty_db():
    """全新空库 → is_truly_empty True。"""
    engine = sa.create_engine(_sync_url())
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("DROP DATABASE IF EXISTS testagent_ce_guard"))
            conn.execute(sa.text("CREATE DATABASE testagent_ce_guard CHARACTER SET utf8mb4"))
            conn.commit()
        guard_engine = sa.create_engine(_db_url("testagent_ce_guard"))
        assert _is_truly_empty(guard_engine) is True
        guard_engine.dispose()
        with engine.connect() as conn:
            conn.execute(sa.text("DROP DATABASE IF EXISTS testagent_ce_guard"))
            conn.commit()
    finally:
        engine.dispose()


@REQUIRES_TEST_MYSQL
def test_non_empty_db_detected():
    """非空库 → is_truly_empty False（不误用于已有业务库）。"""
    engine = sa.create_engine(_sync_url())
    try:
        assert _is_truly_empty(engine) is False
    finally:
        engine.dispose()


@REQUIRES_TEST_MYSQL
def test_precreate_alembic_version_is_wide():
    """预创建的 alembic_version.version_num 为 VARCHAR(64)（修复 2_8R 截断）。"""
    engine = sa.create_engine(_sync_url())
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("DROP DATABASE IF EXISTS testagent_ce_wide"))
            conn.execute(sa.text("CREATE DATABASE testagent_ce_wide CHARACTER SET utf8mb4"))
            conn.commit()
        wide_engine = sa.create_engine(_db_url("testagent_ce_wide"))
        _precreate_alembic_version(wide_engine)
        with wide_engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema='testagent_ce_wide' AND table_name='alembic_version' "
                    "AND column_name='version_num'"
                )
            ).scalar()
        assert row == 64  # VARCHAR(64) 修复 2_8R 截断
        wide_engine.dispose()
        with engine.connect() as conn:
            conn.execute(sa.text("DROP DATABASE IF EXISTS testagent_ce_wide"))
            conn.commit()
    finally:
        engine.dispose()


@REQUIRES_TEST_MYSQL
def test_fresh_empty_bootstrap_reaches_head():
    """端到端：空库 → bootstrap → alembic head + schema 验证。"""
    engine = sa.create_engine(_sync_url())
    boot_db = "testagent_ce_boot_e2e"
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {boot_db}"))
            conn.execute(sa.text(f"CREATE DATABASE {boot_db} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            conn.commit()
        boot_url = _db_url(boot_db)

        from app.services.db_bootstrap import bootstrap

        fp = bootstrap(boot_url)
        assert fp["alembic_version"] == "1b640574c715"  # head
        assert len(fp["tables"]) >= 30  # base + context 表
        # schema 验证：10 张 context 表
        context_tables = [t for t in fp["tables"] if t.startswith("context_")]
        assert len(context_tables) == 10
    finally:
        with engine.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {boot_db}"))
            conn.commit()
        engine.dispose()
