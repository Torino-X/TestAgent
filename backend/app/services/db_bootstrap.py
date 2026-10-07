"""Context Engine / TestAgent 官方数据库初始化（Baseline Bootstrap）。

背景：base chain 的 2_8R 双分支（``b2_8r_b_create_execution_requests``，
33 字符）超过 alembic 默认 ``alembic_version.version_num VARCHAR(32)``，
在**完全空数据库** fresh upgrade 时被 MySQL 静默截断为 32 字符，
导致版本链断裂。已有业务库（增量迁移越过该点）不受影响。

本脚本是**官方初始化路径**：仅适用于完全空数据库，不触碰已有业务库。

流程：
1. 检测"完全空库"（无任何业务表 + 无 alembic_version 表）；
2. 以 VARCHAR(64) 预创建 ``alembic_version``（解决截断根因）；
3. 执行 ``alembic upgrade head``；
4. 输出 schema 指纹（表清单 + 版本）。

约束：
- 非空库 → 拒绝执行（不误用于已有业务库）；
- 不修改任何已发布历史 Migration；
- 不删除历史 Revision；
- 不手工改 alembic_version 假装成功；
- 不触碰生产数据库。

用法：
    python -m app.services.db_bootstrap <database_url>
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlparse

import sqlalchemy as sa


def _is_truly_empty(sync_engine) -> bool:
    """检测完全空库：无任何业务表 + 无 alembic_version 表。"""
    with sync_engine.connect() as conn:
        inspector = sa.inspect(conn)
        tables = inspector.get_table_names()
        # 完全空库 = 没有 alembic_version 也没有业务表
        return len(tables) == 0


def _precreate_alembic_version(sync_engine) -> None:
    """以 VARCHAR(64) 预创建 alembic_version，修复 2_8R 版本截断。"""
    with sync_engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE alembic_version ("
                "version_num VARCHAR(64) NOT NULL, "
                "PRIMARY KEY (version_num)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            )
        )
        conn.commit()


def _run_alembic_upgrade_head(database_url: str) -> int:
    """执行 alembic upgrade head（复用项目 alembic env）。

    通过设置 DATABASE_SYNC_URL 环境变量让 alembic env.py 指向目标库。
    """
    import os

    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_SYNC_URL"] = database_url
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    return 0


def _schema_fingerprint(sync_engine) -> dict:
    """输出 schema 指纹：表清单 + 列数。"""
    with sync_engine.connect() as conn:
        inspector = sa.inspect(conn)
        tables = sorted(inspector.get_table_names())
        cols = {
            t: len(inspector.get_columns(t)) for t in tables
        }
        return {"tables": tables, "column_counts": cols}


def bootstrap(database_url: str) -> dict:
    """官方初始化：空库 → upgrade head → schema 指纹。"""
    sync_url = database_url.replace("mysql+aiomysql://", "mysql+pymysql://", 1)
    sync_url = sync_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    engine = sa.create_engine(sync_url)

    if not _is_truly_empty(engine):
        raise RuntimeError(
            "数据库非空：本脚本仅用于完全空数据库初始化。"
            "已有业务库请使用标准 alembic upgrade 路径。"
        )

    _precreate_alembic_version(engine)
    engine.dispose()

    _run_alembic_upgrade_head(database_url)

    # 重新连接输出指纹
    engine = sa.create_engine(sync_url)
    fingerprint = _schema_fingerprint(engine)
    with engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    engine.dispose()
    fingerprint["alembic_version"] = version
    return fingerprint


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="TestAgent 官方数据库初始化")
    parser.add_argument("database_url", help="数据库连接串（sync 或 async 均可）")
    args = parser.parse_args()

    try:
        fp = bootstrap(args.database_url)
    except Exception as exc:  # noqa: BLE001
        print(f"BOOTSTRAP FAILED: {exc}")
        return 1

    print("BOOTSTRAP OK")
    print(f"alembic_version: {fp.get('alembic_version')}")
    print(f"tables: {len(fp.get('tables', []))}")
    print(f"column_counts: {fp.get('column_counts')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (数据库 + 启动期 Schema 引导):
#
#   链路:
#     应用启动期(app/main.py lifespan):
#       → db_bootstrap.ensure_schema(engine)
#         → 1. create_all() 兜底(开发环境);
#         → 2. 或 Alembic upgrade head (生产环境由 CI 跑过);
#         → 3. CHECK 关键表存在,缺失则打 warning / fail-fast;
#     任何数据库连接失败的根因排查都从这里看。
#
# 关键约束(供开发者速查):
#   - 本模块只在 lifespan startup 调用,**不要**在请求期反复跑;
#   - create_all() **不替代** Alembic 迁移(开发期用,生产期严禁);
#   - 启动失败必须 raise,不允许 silently 跳过(否则任务状态会乱)。
