# Alembic env.py — migration environment configuration

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.db.base import Base

# Import all models so Base.metadata is populated
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from our application settings (sync URL)
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

target_metadata = Base.metadata


def _strip_problematic_sql_mode(current: str) -> str:
    """去掉 NO_ZERO_DATE(老 migration 用了 ``sa.text('now()')`` →
    MySQL ``now()`` 函数 → 返回带年份的 datetime 字符串,在严格 sql_mode 下
    触发 "Invalid default value for 'created_at'")。

    同时去掉 NO_ZERO_IN_DATE(同类)。
    """
    if not current:
        return "STRICT_TRANS_TABLES"
    parts = [p.strip() for p in current.split(",") if p.strip()]
    parts = [p for p in parts if p not in ("NO_ZERO_DATE", "NO_ZERO_IN_DATE")]
    return ",".join(parts) if parts else "STRICT_TRANS_TABLES"


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    from sqlalchemy import event

    @event.listens_for(connectable, "connect")
    def _set_sql_mode(dbapi_connection, _connection_record):
        """连接建立时调整 sql_mode。

        此前在 context.configure 前直接对连接执行 SET SESSION sql_mode，
        会让 alembic 后续的 alembic_version 写入静默丢失（版本行不落库）。
        改用连接事件：sql_mode 每次连接初始化即生效，不干扰 alembic 事务。
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT @@SESSION.sql_mode")
            current = cursor.fetchone()[0] or ""
            new_mode = _strip_problematic_sql_mode(current)
            cursor.execute(f"SET SESSION sql_mode = '{new_mode}'")
        finally:
            cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # MySQL DDL 隐式提交。外层 begin_transaction 会让 alembic 误以为
            # 版本写入在同一事务内，而 DDL 已隐式提交导致版本行丢失。
            # 让 alembic 自己管理每个迁移的事务，版本才能正确推进。
            transaction_per_migration=True,
        )
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
