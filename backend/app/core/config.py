"""Application configuration loaded from environment variables.

Database URL resolution (priority order):
  1. DATABASE_SYNC_URL  – used by Alembic & sync scripts
  2. DATABASE_ASYNC_URL – used by FastAPI async engine
  3. DATABASE_URL       – fallback; sync uses it directly,
                          async converts pymysql → aiomysql
  4. Assembled from DATABASE_HOST / PORT / USER / PASSWORD / NAME

Credentials must NEVER be hardcoded in this file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings.

    Values are read from environment variables / .env file. Database-level
    config (model_configs, knowledge_base_configs, system_configs) is read at
    runtime via the appropriate repositories.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────────────────
    app_name: str = "TestAgent"
    app_env: str = "development"
    debug: bool = False
    api_prefix: str = "/api"
    secret_key: str = "change_me"
    access_token_expire_minutes: int = 1440

    # Structured logging. Process environment remains authoritative over
    # backend/.env through the application's existing Pydantic settings flow.
    log_level: str = "INFO"
    log_console_enabled: bool = True
    log_file_enabled: bool = True
    log_dir: str = "./logs"
    log_file_max_bytes: int = 50 * 1024 * 1024
    log_file_backup_count: int = 10
    log_redaction_enabled: bool = True
    log_level_http: str = "INFO"
    log_level_application: str = "INFO"
    log_level_agent: str = "INFO"
    log_level_context: str = "INFO"
    log_level_integration: str = "INFO"
    log_level_security: str = "INFO"
    log_schema_version: str = "2.0"
    log_sampling_enabled: bool = True
    log_success_sample_rate: float = 1.0
    log_health_sample_rate: float = 0.01
    log_rate_limit_enabled: bool = True
    log_rate_limit_window_seconds: int = 60
    log_max_event_bytes: int = 32 * 1024
    log_incident_bundle_enabled: bool = True
    otel_enabled: bool = False
    otel_service_name: str = "testagent-backend"
    otel_exporter_otlp_endpoint: str = ""
    otel_sample_rate: float = 1.0

    # ── Database ───────────────────────────────────────────────────────
    # Prioritised URLs — omit to fall back to DATABASE_URL or assembly
    database_sync_url: str = ""
    database_async_url: str = ""
    database_url: str = ""

    # Individual fields (used only when all URLs above are empty)
    database_host: str = "localhost"
    database_port: int = 3306
    database_user: str = "root"
    database_password: str = ""
    database_name: str = "testagent"

    # ── CORS ───────────────────────────────────────────────────────────
    cors_origins: List[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5310",
        "http://127.0.0.1:5310",
        "http://localhost:5313",
        "http://127.0.0.1:5313",
        "http://localhost:5318",
        "http://127.0.0.1:5318",
    ]

    # ── File Storage ───────────────────────────────────────────────────
    local_storage_path: str = "./data"
    upload_max_bytes: int = 50 * 1024 * 1024
    # A public marketplace template used when a test-plan request includes a
    # requirement document but no user-selected output template.  Empty
    # disables automatic default-template selection.
    default_test_plan_template_name: str = "00_PlanWise_QA_测试方案模板"
    # Durable user files and generated artifacts are stored in a private OSS
    # bucket.  The application keeps only opaque object keys in the database.
    oss_endpoint: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_bucket_name: str = ""
    oss_prefix: str = "testagent"

    # DOCX preview uses a real Office layout engine. Durable source files stay
    # in OSS; conversion uses only a request-scoped temporary directory.
    document_preview_office_bin: str = ""
    document_preview_office_timeout_seconds: int = 90

    # ── OCR Worker (子进程配置，2026-07-14 路线 B) ─────────────────
    # OCR 服务被拆为 ``OcrProcessClient`` 主进程 + ``ocr_worker.py`` 子进程
    # 架构。这些字段控制 spawn 时的行为：
    #   - ocr_threads: BLAS 线程数，spawn 时注入到 worker env（覆盖 os.environ）
    #   - ocr_subprocess_timeout: ready 等待 / 单次 predict 超时
    #   - ocr_profile: lightweight|high_accuracy|auto
    #   - ocr_stderr_prefix: 主进程 re-log 时加的前缀
    ocr_threads: int = 2
    ocr_subprocess_timeout: float = 30.0
    ocr_profile: str = "auto"
    ocr_stderr_prefix: str = "[ocr-worker]"

    # ── Phase 2.6: Multi-Worker runtime config ─────────────────────────
    # AGENT_RUNTIME_REDIS_URL: opt-in cross-worker pub/sub;未设 → 退化 InMemory
    agent_runtime_redis_url: str = ""
    # AGENT_RUNTIME_MULTIWORKER_INTEGRATION: 开启 multi-worker 集成测试
    # (默认 False; 集成测试本地通过 env 开启)
    agent_runtime_multiworker_integration: bool = False

    # ── Agent Runtime (Phase 2.0 LangGraph 基础设施) ────────────────
    # 默认 False。设为 True 时,AgentEngineDispatcher 才会接受
    # engine_type="langgraph" 的任务;测试环境 (PYTEST_CURRENT_TEST) 自动启用。
    agent_runtime_langgraph_enabled: bool = False

    # ── Phase 2.8R-D: Graph 默认版本 ───────────────────────────────
    # AGENT_RUNTIME_DEFAULT_GRAPH_VERSION: 新 LangGraph 任务写入的
    # ``agent_tasks.graph_version`` 默认值。必须是已注册版本 (v1/v2/v3),
    # 未知值会在 MessageService 落库时被显式拒绝。Phase 2.8R-D 默认 v3。
    agent_runtime_default_graph_version: str = "v3"
    # Tool / agent public narrative verbosity. User-facing settings only
    # control visibility; this detail level is developer-operated via .env.
    agent_runtime_narrative_detail_level: str = "standard"

    # ── Phase 2.8A: Production dispatch + Postgres Checkpointer ──
    # AGENT_RUNTIME_PRODUCTION_DISPATCH_ENABLED:
    #   默认 False。只有显式启用且依赖健康时才执行 LangGraph；否则 fail-closed。
    agent_runtime_production_dispatch_enabled: bool = False
    # AGENT_RUNTIME_POSTGRES_URL:
    #   postgresql://user:pass@host:5432/dbname?sslmode=disable
    #   (legacy postgresql+asyncpg://...?...ssl=false is normalized at runtime)
    #   留空 → MemorySaver(守禁令:LangGraph 生产任务不得使用纯 MemorySaver;
    #   lifespan 探测失败时强制 production_dispatch_enabled=False)。
    agent_runtime_postgres_url: str = ""
    # AGENT_RUNTIME_POSTGRES_POOL_SIZE: psycopg async connection pool 大小
    agent_runtime_postgres_pool_size: int = 5
    # AGENT_RUNTIME_POSTGRES_SETUP_ON_START:
    #   lifespan 启动时调用 AsyncPostgresSaver.setup() 自动建表(生产推荐 True)
    agent_runtime_postgres_setup_on_start: bool = True
    # ── CE-05: opaque cursor 加密密钥 ──────────────────────────────
    # CONTEXT_CURSOR_SECRET: AES-256-GCM cursor 密钥材料(要求 ≥32 字节)。
    # 由 cursor.py 经 HKDF-SHA256 派生为恰好 32 字节；未配置 → cursor 端点 503。
    context_cursor_secret: str = ""

    # ── Phase 2.8B: 跨 worker InFlight Redis lock + Postgres 实证 ──
    # AGENT_RUNTIME_REDIS_INFLIGHT_URL:
    #   redis://user:pass@host:port/db
    #   留空 → 跨 worker 守护降级到进程级 InFlightTaskRegistry(2.8A)
    #   Redis 不可用 → graceful degrade to in-memory(守禁令 #31)
    agent_runtime_redis_inflight_url: str = ""
    # AGENT_RUNTIME_REDIS_INFLIGHT_TTL_SECONDS:
    #   Redis SET NX EX 的 TTL;默认 1800s(30 分钟)。
    #   worker 崩溃后其他 worker 接管无需人工清理;TTL 兜底过期(守禁令 #32)
    agent_runtime_redis_inflight_ttl_seconds: int = 1800
    # AGENT_RUNTIME_REDIS_INFLIGHT_LOCK_PREFIX:
    #   Redis keyspace 前缀;默认 "inflight:"
    agent_runtime_redis_inflight_lock_prefix: str = "inflight:"

    # ── Phase 2.8C: 动态 Agent API 入口总开关 ──
    # AGENT_RUNTIME_DYNAMIC_AGENT_API_ENABLED:
    #   控制 ApiDispatcher.dispatch_incremental_task / dispatch_incremental_resume
    #   / dispatch_repair_task 是否挂真实入口。
    #   默认 False;生产必须显式 opt-in(守禁令 #31:不允许 dynamic_agent_api
    #   在 production 默认开启);测试 sandbox (PYTEST_CURRENT_TEST) auto-on。
    #   与 production_dispatch_enabled 独立:前者控制动态 Agent 入口,后者
    #   控制 LangGraph 生产路径。
    agent_runtime_dynamic_agent_api_enabled: bool = False

    # Phase 2.8R-B + 2.8R-I:AgentExecutionWorker 后台 Worker 启动开关
    # 默认 True(自动启动);设为 False 用于单进程调试 / 测试场景。
    # 生产必须保持 True,否则 Outbox 队列永远没人领,任务卡在 queued 状态。
    agent_runtime_worker_autostart: bool = True
    # Worker poll 间隔(秒);1.0 是默认,测试场景可设更短。
    agent_runtime_worker_poll_interval_seconds: float = 1.0

    # ── Phase 1: Business Cache Redis ──────────────────────────────
    # 独立于 Runtime Redis 的业务 Query Cache;详见设计文档 §4.2 + §30
    # 与 ZCode 实施提示词 §6。原则:
    #   - MySQL 仍是 SoT;Cache Redis 不可用 → DB fallback (限流)
    #   - 与 Runtime Redis 实例隔离,避免 eviction 互踩
    #   - 默认 allkeys-lfu;每 cache key 都带 TTL
    # CACHE_REDIS_ENABLED: 总开关;false → 全部 Domain Cache bypass
    cache_redis_enabled: bool = True
    # CACHE_REDIS_URL: Business Cache Redis 连接串;空字符串 → bypass
    cache_redis_url: str = ""
    # 连接池上限 (单 worker 共享一个 client)
    cache_redis_max_connections: int = 100
    # fail-fast 超时设置
    cache_redis_connect_timeout_ms: int = 200
    cache_redis_socket_timeout_ms: int = 100
    cache_redis_health_check_interval_seconds: int = 30
    # Key namespace 前缀;与 Runtime Redis 的 "inflight:" / EventBus
    # keyspace 完全隔离
    cache_key_prefix: str = "ta"
    cache_schema_version: str = "v1"
    # Distributed cache-fill lock (热点 spec 才用;绝大多数 spec 关掉)
    cache_lock_ttl_ms: int = 2000
    cache_lock_wait_ms: int = 200
    # DB fallback 并发闸门;cache loader 走 DB 时受此限流
    cache_db_fallback_max_concurrency: int = 5
    # Circuit Breaker:连续错误开 breaker,避免每个请求都等 socket timeout
    cache_breaker_failure_threshold: int = 5
    cache_breaker_open_seconds: int = 5
    # Value size 上限:超过即跳过缓存 (256 KiB 默认;防 MEDIUMTEXT 污染)
    cache_max_value_bytes: int = 262144
    # CacheMaintenanceWorker (library purge 等) 周期;与设计文档 §14.4
    # /handover §1.3 一致
    cache_maintenance_interval_seconds: float = 60.0

    # ── Domain feature flags (粒度关闭某个 Domain Cache) ──
    # 默认全部 True;测试 / 灰度时单独关一个 domain 不影响其它
    cache_auth_enabled: bool = True
    cache_cfg_enabled: bool = True
    cache_conv_enabled: bool = True
    cache_task_enabled: bool = True
    cache_lib_enabled: bool = True
    cache_ctx_enabled: bool = True
    cache_sem_enabled: bool = True
    cache_fb_enabled: bool = True  # Feedback (Business Cache Redis port 6380)

    # ── URL properties ─────────────────────────────────────────────────

    def _build_sync_url(self) -> str:
        """Build a sync MySQL URL from individual fields."""
        return (
            f"mysql+pymysql://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}"
            f"/{self.database_name}?charset=utf8mb4"
        )

    @property
    def sync_database_url(self) -> str:
        """Sync URL used by Alembic, check_db, and synchronous sessions.

        Priority: DATABASE_SYNC_URL → DATABASE_URL → assembled
        """
        if self.database_sync_url:
            return self.database_sync_url
        if self.database_url:
            return self.database_url
        return self._build_sync_url()

    @property
    def async_database_url(self) -> str:
        """Async URL used by FastAPI async engine.

        Priority: DATABASE_ASYNC_URL → (DATABASE_URL converted) → assembled
        """
        if self.database_async_url:
            return self.database_async_url
        sync = self.sync_database_url
        return sync.replace("mysql+pymysql://", "mysql+aiomysql://", 1)

    @property
    def safe_database_url(self) -> str:
        """Return a sync URL with password masked for logging."""
        url = self.sync_database_url
        try:
            at_idx = url.rindex("@")
            colon_idx = url.rindex(":", 0, at_idx)
            return url[: colon_idx + 1] + "******" + url[at_idx:]
        except ValueError:
            return url

    @property
    def uploads_path(self) -> Path:
        return Path(self.local_storage_path) / "uploads"

    @property
    def artifacts_path(self) -> Path:
        return Path(self.local_storage_path) / "artifacts"

    @property
    def temp_path(self) -> Path:
        return Path(self.local_storage_path) / "temp"

    @property
    def oss_is_configured(self) -> bool:
        return bool(
            self.oss_endpoint
            and self.oss_access_key_id
            and self.oss_access_key_secret
            and self.oss_bucket_name
        )

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
