# TestAgent Checkpointer与持久化 技术实现文档

> **配套文档**
> - 总体技术方案: [02_TestAgent_项目总体技术方案.md](../02_TestAgent_项目总体技术方案.md)
> - 源码导读: [03_TestAgent_项目实现原理与源码导读.md](../03_TestAgent_项目实现原理与源码导读.md)
> - 证据索引: [01_TestAgent_项目技术方案证据索引.md](../01_TestAgent_项目技术方案证据索引.md)
> - Context Engine 3.0: [10_TestAgent_ContextEngine3.0_技术实现文档.md](10_TestAgent_ContextEngine3.0_技术实现文档.md)
> - 测试方案生成主图: [12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md](12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)
> - SSE + LiveEventBus: [17_TestAgent_SSE与LiveEventBus_技术实现文档.md](17_TestAgent_SSE与LiveEventBus_技术实现文档.md)

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`
**最后对齐**: 与 Phase 2.8A（Postgres Checkpointer）+ Phase 2.8B（跨 worker InFlight + 实证 helper）+ Phase 2.8R-C（严格化）+ Phase 2.8R-D（FIX-B Pool）+ Phase 2.8R-F（BusinessBudgets）同步

> 本文档面向**接手 Checkpointer 与持久化模块**的开发者，覆盖 **Postgres Checkpointer 工厂 + ProbeRouter 启动探测 + RedisInFlightRegistry 跨 worker 锁 + CrossWorkerRecovery 扫描 + BusinessBudgets 业务循环预算 + RecursionLimitConfig + Alembic 迁移 + AgentExecutionWorker 多 worker 调度**。
> 行号以当前 `dev_3.0` HEAD 为准。

---

## 1. 文档说明

### 1.1 模块定位

Checkpointer 与持久化是 TestAgent 第二阶段（LangGraph v3）的**多 worker 协调与故障恢复**核心：

- **职责 1**：Postgres Checkpointer 工厂（AsyncConnectionPool 替代单连接，FIX-B）
- **职责 2**：ProbeRouter 启动期统一探测（Postgres + LiveEventBus + Redis InFlight，3 类并发）
- **职责 3**：RedisInFlightRegistry 跨 worker 并行调度守护（SET NX + TTL + owner 校验）
- **职责 4**：CrossWorkerRecovery 扫描可恢复 checkpoints
- **职责 5**：PostgresIntegration 实证 helper（4 张核心表 + round-trip + count）
- **职责 6**：BusinessBudgets 业务循环预算（11 路径 PATH_MEASUREMENTS）
- **职责 7**：RecursionLimitConfig env-driven override
- **职责 8**：Alembic 迁移（多 worker 部署支持）

### 1.2 适用读者

| 角色 | 期望收获 |
|---|---|
| **DevOps / SRE** | ProbeRouter 启动探测 + 强制降级 + 4 张核心表验证 |
| **多 Worker 维护者** | RedisInFlightRegistry SET NX + TTL + owner 校验 |
| **故障恢复维护者** | CrossWorkerRecovery 扫描 + release_orphaned_lock |
| **业务循环调优者** | BusinessBudgets 11 路径矩阵 + recursion_limit 计算 |
| **Postgres 管理员** | normalize_postgres_conn_string + FIX-B Pool + Windows 兼容 |

### 1.3 当前状态

- **1,666 行 persistence 代码**（7 文件）+ **399 行 budgets/recursion 代码** + **94 行 state_migration** = **2,159 行后端核心**
- **Phase 2.8A Postgres Checkpointer 完整闭环**
- **Phase 2.8B 跨 worker InFlight + Recovery 完整闭环**
- **Phase 2.8R-C 严格化**（Postgres 不可用 → CheckpointerUnavailableError）
- **Phase 2.8R-D FIX-B**（AsyncConnectionPool 替代单连接）
- **Phase 2.8R-F BusinessBudgets + RecursionLimit**（11 路径测量 + env override）

---

## 2. 总体架构

```mermaid
flowchart TB
    subgraph L1["lifespan 启动期"]
        P1[ProbeRouter.probe_at_startup]
        P2[Postgres probe_postgres_checkpointer]
        P3[LiveEventBusProbe.resolve_with_health_check]
        P4[Redis InFlight SET NX + DEL]
    end
    subgraph L2["ProbeReport 决策"]
        PR[ProbeReport<br/>postgres_ok / eventbus_kind /<br/>redis_inflight_ok /<br/>production_dispatch_forced_off /<br/>langgraph_readiness]
    end
    subgraph L3["Checkpointer 工厂"]
        CF[build_checkpointer]
        IM[build_inmemory_checkpointer<br/>MemorySaver]
        PG[build_postgres_checkpointer<br/>AsyncPostgresSaver +<br/>AsyncConnectionPool FIX-B]
    end
    subgraph L4["多 Worker 协调"]
        RIF[RedisInFlightRegistry<br/>SET NX + TTL + owner]
        CWR[CrossWorkerRecovery<br/>scan_recoverable_checkpoints]
    end
    subgraph L5["业务循环预算"]
        BB[BusinessBudgets<br/>11 路径 PATH_MEASUREMENTS]
        RLC[RecursionLimitConfig<br/>AGENT_RUNTIME_RECURSION_LIMIT]
    end
    subgraph L6["Postgres 实证"]
        PI[verify_postgres_4_tables]
        RT[roundtrip_checkpoint]
        CT[count_checkpoints]
    end
    subgraph L7["AgentExecutionWorker"]
        AEW[多 worker Outbox +<br/>SKIP LOCKED 领取]
    end
    subgraph L8["Alembic 迁移"]
        AM[agent_execution_requests /<br/>agent_events /<br/>checkpoints /<br/>context_snapshots]
    end

    P1 --> P2 & P3 & P4
    P1 --> PR
    PR --> CF
    CF --> IM
    CF --> PG
    PR --> RIF
    AEW --> RIF
    AEW --> CF
    AEW --> AM
    CWR --> RIF
    CWR --> AM
    BB --> RLC
    PI --> AM
    RT --> AM
    CT --> AM
```

---

## 3. 文件结构（实际 `wc -l` 验证）

### 3.1 backend/app/agent_runtime/persistence/（7 文件 / 1,666 行）

| 文件 | 行数 | 职责 |
|---|---|---|
| `probe_router.py` | **338** | **ProbeRouter 启动期统一探测**（3 类并发 + ProbeReport）|
| `redis_inflight_registry.py` | **251** | **RedisInFlightRegistry 跨 worker 锁**（SET NX + TTL + owner 校验）|
| `postgres_checkpointer.py` | **298** | **Postgres Checkpointer 工厂**（FIX-B Pool + Windows 兼容）|
| `cross_worker_recovery.py` | **243** | **跨 worker 恢复扫描**（scan_recoverable + release_orphaned）|
| `checkpointer_factory.py` | **212** | **build_checkpointer 统一入口**（memory / postgres + CheckpointerUnavailableError）|
| `postgres_integration.py` | **191** | **Postgres 实证 helper**（verify_4_tables + roundtrip + count）|
| `checkpoint_writer.py` | 65 | **CheckpointWriter 双写 hook**（节点完成后 ContextStore）|
| `__init__.py` | 68 | 模块导出 |

### 3.2 配套文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `business_budgets.py` | **202** | **BusinessBudgets**（11 路径 PATH_MEASUREMENTS + recursion_limit 计算）|
| `recursion_limit_config.py` | **97** | **RecursionLimitConfig**（AGENT_RUNTIME_RECURSION_LIMIT env override）|
| `checkpoint_state_migration.py` | 94 | 跨 worker checkpoint 状态迁移 |
| `event_publisher.py` | 211 | 双写适配器（已在 A7 文档）|

### 3.3 services/ + api/v1/ + alembic/

| 文件 | 行数 | 职责 |
|---|---|---|
| `services/agent_execution_worker.py` | **595** | **多 worker Outbox + SKIP LOCKED 领取** |
| `api/v1/agent_tasks.py` | 1422 | API 端点（含 SSE stream_events）|
| `alembic/versions/*` | – | 19 个迁移（含 CE 4 张表 + agent_events idempotency） |

---

## 4. Postgres Checkpointer 工厂（`postgres_checkpointer.py` 298 行）

### 4.1 FIX-B: AsyncConnectionPool 替代单连接

```python
"""Postgres Checkpointer 工厂与健康探测 — Phase 2.8A。

设计要点:
* 只暴露 ``probe_postgres_checkpointer`` + ``build_postgres_checkpointer`` 两个
  异步入口;不在模块级直接 import langgraph.checkpoint.postgres (避免未装
  asyncpg 的环境启动失败)。
* ``probe_postgres_checkpointer(url) -> bool``:lifespan 启动期探测;失败
  立即返回 False,绝不抛错。
* ``build_postgres_checkpointer(url, *, setup=True) -> AsyncPostgresSaver | None``:
  连接 + 可选建表,失败返回 None(callers 必须接受 None 兜底 → MemorySaver)。
* Windows 兼容:``asyncio.run()`` 默认 ``ProactorEventLoop`` 下 psycopg 报错;
  在调用 ``from_conn_string`` 之前切换 ``SelectorEventLoop`` 即可。

守禁令:
* 不在 GraphState 放 Session/LLMClient 等;本模块只返回 checkpointer 实例。
"""
```

### 4.2 `normalize_postgres_conn_string()` — libpq 兼容

```python
def normalize_postgres_conn_string(url: str) -> str:
    """Convert legacy SQLAlchemy Postgres URLs to psycopg-compatible URLs.

    ``AsyncPostgresSaver`` uses psycopg directly.  It accepts libpq-style
    ``postgresql://`` URLs, while existing application settings may still use
    SQLAlchemy's ``postgresql+asyncpg://`` scheme and ``ssl=false`` option.
    """
    url = str(url or "").strip().strip("'\"\r\n")
    if not url:
        return ""
    parsed = urlsplit(url)
    # SQLAlchemy accepts driver-qualified schemes (for example
    # ``postgresql+asyncpg://`` and ``postgresql+psycopg://``), while psycopg
    # expects the libpq form without the ``+driver`` suffix.
    scheme = "postgresql" if parsed.scheme.startswith("postgresql+") else parsed.scheme
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    has_sslmode = any(key.lower() == "sslmode" for key, _ in query_items)

    if not has_sslmode:
        normalized_items = []
        for key, value in query_items:
            if key.lower() == "ssl" and value.lower() in {"0", "false", "no", "off"}:
                normalized_items.append(("sslmode", "disable"))
            elif key.lower() == "ssl" and value.lower() in {"1", "true", "yes", "on"}:
                normalized_items.append(("sslmode", "require"))
            else:
                normalized_items.append((key, value))
        query_items = normalized_items

    return urlunsplit((scheme, parsed.netloc, parsed.path, urlencode(query_items), parsed.fragment))
```

**关键变换**：
- `postgresql+asyncpg` / `postgresql+psycopg` → `postgresql`（libpq 形式）
- `ssl=false` → `sslmode=disable`（psycopg 兼容）
- `ssl=true` → `sslmode=require`

### 4.3 `_ensure_selector_event_loop()` — Windows 兼容

```python
def _ensure_selector_event_loop() -> None:
    """Windows 兼容:检查当前 event loop policy,如果是 Proactor 则提示警告。

    Phase 2.8R-D:在 lifespan 中也主动尝试把 policy 切到 Selector(限一次;
    后续运行中切换无副作用)。
    """
    if sys.platform == "win32":
        try:
            current = asyncio.get_event_loop_policy().__class__.__name__
            if current != "WindowsSelectorEventLoopPolicy":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                logger.info("Postgres Checkpointer: 已切换到 WindowsSelectorEventLoopPolicy ...")
        except RuntimeError:
            loop = asyncio.get_event_loop_policy().get_event_loop()
            if loop.__class__.__name__ == "ProactorEventLoop":
                logger.warning("Postgres Checkpointer: Windows ProactorEventLoop detected — psycopg async requires SelectorEventLoop.")
    else:
        # Linux/CI:SelectorEventLoop 是默认
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
            if loop.__class__.__name__ == "ProactorEventLoop":
                logger.warning(...)
        except RuntimeError:
            pass
```

### 4.4 `build_postgres_checkpointer()` — FIX-B Pool

```python
async def build_postgres_checkpointer(
    url: str,
    *,
    setup: bool = True,
    timeout: float = 5.0,
) -> Optional["object"]:
    if not url or not url.strip():
        return None

    _ensure_selector_event_loop()
    conn_string = normalize_postgres_conn_string(url)

    async def _inner():
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # FIX-B: 使用 AsyncConnectionPool 而非单连接。
        # 单连接 AsyncPostgresSaver.from_conn_string 持有单一 AsyncConnection；
        # 当 PostgreSQL 服务器关闭该连接(idle_session_timeout / 网络中断)后，
        # saver 仍引用 broken connection → 后续 get_tuple/put 全部 OperationalError。
        async def _pg_health_check(conn) -> None:
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    await cur.fetchone()
            except Exception:
                raise  # 任何异常都让 pool 丢弃此连接

        pool = AsyncConnectionPool(
            conn_string,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            min_size=1,
            max_size=10,
            max_idle=300.0,
            max_lifetime=1800.0,
            timeout=timeout,
            reconnect_timeout=timeout,
            check=_pg_health_check,
        )
        await pool.open()
        cp = AsyncPostgresSaver(conn=pool)
        if setup:
            try:
                await asyncio.wait_for(cp.setup(), timeout=timeout)
            except Exception as exc:
                await pool.close()
                return None
        # 绑定 pool 到 saver,让 caller ``await saver.aclose()`` 关闭 pool。
        setattr(cp, "_pg_pool", pool)
        return cp
    ...
```

**FIX-B 关键**：
- **AsyncConnectionPool** 替换单连接（防 idle_session_timeout 后 broken connection）
- **min_size=1 / max_size=10**（连接池大小）
- **max_idle=300s / max_lifetime=1800s**（连接生命周期管理）
- **check 回调**（`SELECT 1` 健康检查）
- **`_pg_pool` 属性绑定**（aclose 时一并关闭）

### 4.5 `aclose_postgres_checkpointer()`

```python
async def aclose_postgres_checkpointer(checkpointer: Optional["object"]) -> None:
    """Safely close a checkpointer returned by ``build_postgres_checkpointer``."""
    if checkpointer is None:
        return
    # FIX-B: 优先关闭 pool(_pg_pool)
    pool = getattr(checkpointer, "_pg_pool", None)
    try:
        if pool is not None and hasattr(pool, "close"):
            await pool.close()
            return
    except Exception:
        ...
    cm = getattr(checkpointer, "_pg_cm", None)
    try:
        if cm is not None:
            await cm.__aexit__(None, None, None)
            return
    except Exception:
        ...
    try:
        if hasattr(checkpointer, "aclose"):
            await checkpointer.aclose()
    except Exception:
        ...
```

---

## 5. Checkpointer 统一工厂（`checkpointer_factory.py` 212 行）

### 5.1 守禁令

```python
"""Checkpointer 工厂 — Phase 2.8R-C 严格化。

设计要点(对应 docs/35 §3 + 验收三 / 验收四):
  * 工厂函数 ``build_checkpointer`` 接收 ``backend`` 参数:
      - ``backend="memory"`` → ``MemorySaver``(显式,测试用)
      - ``backend="postgres"`` → ``AsyncPostgresSaver``(强制,生产用);
        **失败时抛 ``CheckpointerUnavailableError``**(不再静默 fallback MemorySaver)。
  * ``build_inmemory_checkpointer`` 保留作为向后兼容入口,但内部统一走
    ``build_checkpointer(backend="memory")``。
  * ``build_postgres_checkpointer`` 走 raw SQL,失败抛 CheckpointerUnavailableError,
    由 main.py lifespan 调用。
  * ``aclose_checkpointer`` 统一关资源。

守禁令映射:
  * 守 #18 → LangGraph 任务 Postgres 不可用 → 强制 production_dispatch_forced_off=True
    (probe_router 已实现,但 **silent fallback MemorySaver** 仍存在 → 删除)
  * 守 #21 → 任何 LangGraph 任务不得 downgrade 到 MemorySaver
"""
```

### 5.2 `build_checkpointer()` 统一入口

```python
async def build_checkpointer(
    *,
    backend: str | None = None,
    url: str | None = None,
    timeout: float = 10.0,
) -> Any:
    """统一工厂入口。

    Args:
        backend: ``"memory"`` 或 ``"postgres"``;若 None,根据 env var
            ``AGENT_RUNTIME_CHECKPOINTER_BACKEND`` 决定(默认 ``"memory"``)。
        url: 仅 postgres 用;若 None → env ``AGENT_RUNTIME_POSTGRES_URL``。

    Returns:
        ``MemorySaver`` 或 ``AsyncPostgresSaver`` 实例。

    Raises:
        CheckpointerUnavailableError: postgres backend 但连接失败。
    """
    chosen = (backend or os.environ.get("AGENT_RUNTIME_CHECKPOINTER_BACKEND") or "memory").strip().lower()

    if chosen in {"", "memory", "inmemory", "in_memory"}:
        return build_inmemory_checkpointer()

    if chosen in {"postgres", "postgresql", "pg"}:
        return await build_postgres_checkpointer(url=url, timeout=timeout)

    raise CheckpointerUnavailableError(reason="unknown_backend", detail={...})
```

### 5.3 `build_postgres_checkpointer()` 严格化

```python
async def build_postgres_checkpointer(
    url: str | None = None,
    *,
    setup: bool = True,
    timeout: float = 10.0,
) -> Any:
    """构建 ``AsyncPostgresSaver``(Phase 2.8A 已有,在这里只 thin-wrap)。

    Phase 2.8R-C:失败抛 ``CheckpointerUnavailableError``(原行为是返回 None + log
    + 让 caller 兜底 MemorySaver,违反守 #18)。
    """
    target_url = url or os.environ.get("AGENT_RUNTIME_POSTGRES_URL") or ""
    if not target_url:
        raise CheckpointerUnavailableError(
            reason="empty_postgres_url",
            detail={"reason": "empty_postgres_url", "hint": "set AGENT_RUNTIME_POSTGRES_URL or pass url"},
        )

    # 先 probe,再 build
    try:
        ok = await asyncio.wait_for(probe_postgres_checkpointer(target_url, timeout=timeout), timeout=timeout + 1.0)
    except Exception as exc:
        raise CheckpointerUnavailableError(reason="probe_failed", detail={...}) from exc

    if not ok:
        raise CheckpointerUnavailableError(reason="probe_unhealthy", detail={...})

    try:
        cp = await _raw_build(target_url, setup=setup, timeout=timeout)
        if cp is None:
            raise CheckpointerUnavailableError(reason="build_failed", detail={...})
        return cp
    except CheckpointerUnavailableError:
        raise
    except Exception as exc:
        raise CheckpointerUnavailableError(reason="build_failed", detail={...}) from exc
```

**Phase 2.8R-C 关键变化**：
- **失败抛 CheckpointerUnavailableError**（不再返回 None + log + fallback MemorySaver）
- **守 #18 严格化**：Postgres 不可用 → 强制 production_dispatch_forced_off=True
- **守 #21**：任何 LangGraph 任务不得 downgrade 到 MemorySaver

### 5.4 `aclose_checkpointer()`

```python
async def aclose_checkpointer(checkpointer: Any | None) -> None:
    """关闭 checkpointer(兼容 Postgres + MemorySaver;None 安全)。

    FIX-B:AsyncPostgresSaver 无 ``aclose`` 方法;若 saver 持有 ``_pg_pool``
    (FIX-B 新增)则关闭 pool。否则退回 ``checkpointer.aclose()``(MemorySaver)。
    """
    if checkpointer is None:
        return
    pool = getattr(checkpointer, "_pg_pool", None)
    if pool is not None and hasattr(pool, "close"):
        try:
            await pool.close()
            return
        except Exception:
            ...
    try:
        await checkpointer.aclose()
    except Exception:
        ...
```

### 5.5 URL 脱敏

```python
def _redact_url(url: str) -> str:
    """掩码 URL 中 password,避免日志泄漏。"""
    if not url:
        return ""
    try:
        # postgresql+psycopg://user:pass@host:port/db
        if "://" in url and "@" in url:
            scheme_userpass, host_part = url.split("://", 1)
            if "@" in host_part:
                user_pass, rest = host_part.split("@", 1)
                if ":" in user_pass:
                    user, _ = user_pass.split(":", 1)
                    return f"{scheme_userpass}://{user}:***@{rest}"
        return url
    except Exception:
        return "<redact-failed>"
```

---

## 6. ProbeRouter 启动探测（`probe_router.py` 338 行）

### 6.1 3 类并发探测

```python
"""Lifespan 启动期统一探测 — Phase 2.8A + 2.8B。

设计要点(对应 docs/29 §10 + docs/30 §4):
* ``probe_at_startup`` 在 lifespan 中跑一次,聚合三件事:
  - Postgres Checkpointer 健康(probe_postgres_checkpointer)
  - LiveEventBus 模式(LiveEventBusProbe.resolve_with_health_check)
  - Redis InFlight 锁健康(Phase 2.8B 新增:_probe_redis_inflight)
* 返回 ``ProbeReport`` dataclass — 决策逻辑完全在调用方(避免模块间循环)。
* **Postgres 失败 → 强制 production_dispatch_enabled=False**(守禁令 #18)。
* **Redis InFlight 失败 → 跨 worker 守护降级**到进程级 InFlightTaskRegistry。
* 所有探测均 ≤ 5s,绝不阻塞启动(守禁令 #28)。
"""
```

### 6.2 `ProbeReport` 13 字段

```python
@dataclass(frozen=True)
class ProbeReport:
    """Phase 2.8A+2.8B lifespan 探测报告 — 进程级只读事实。"""

    # Postgres
    postgres_ok: bool
    postgres_url_echo: str  # 脱敏的 url
    postgres_latency_ms: int

    # LiveEventBus
    eventbus_kind: str  # "Redis" / "InMemory" / "InMemoryDegraded"
    eventbus_url_echo: str
    redis_ok: bool  # True = 期望 Redis 且真正拿到 Redis,或未配置(InMemory OK)

    # 整体就绪
    ready: bool
    production_dispatch_forced_off: bool

    # Phase 2.8B 新增
    redis_inflight_ok: bool = False
    redis_inflight_url_echo: str = ""
    in_flight_lock_backend: str = "InMemory"  # "Redis" / "InMemory" / "InMemoryDegraded"
    in_flight_lock_ttl_seconds: int = 1800

    # Phase 2.8R-D 新增
    langgraph_readiness: bool = True
    checkpointer_type: str = "none"  # "AsyncPostgresSaver" / "MemorySaver" / "none"

    warnings: tuple[str, ...] = field(default_factory=tuple)
```

### 6.3 `probe_at_startup()` 主入口

```python
async def probe_at_startup(
    *,
    postgres_url: str,
    redis_url: str,
    redis_inflight_url: str = "",
    redis_inflight_ttl_seconds: int = 1800,
    postgres_timeout: float = 5.0,
    overall_timeout: float = 8.0,
) -> ProbeReport:
    """Phase 2.8A+2.8B 启动期一次性探测 — lifespan 调用。

    整个探测在 ``overall_timeout`` 内必须完成(超时则返回"全部 fail"报告)。
    Postgres / LiveEventBus / Redis InFlight 并发探测,各自有独立超时。
    """
    pg_url_echo = _mask_url(postgres_url)
    eb_url_echo = _mask_url(redis_url)
    ri_url_echo = _mask_url(redis_inflight_url)

    async def _probe_postgres() -> tuple[bool, int]:
        if not postgres_url or not postgres_url.strip():
            return False, 0
        start = time.monotonic()
        try:
            ok = await asyncio.wait_for(
                probe_postgres_checkpointer(postgres_url, timeout=postgres_timeout),
                timeout=postgres_timeout + 1.0,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            return ok, latency_ms
        except asyncio.TimeoutError:
            return False, int((time.monotonic() - start) * 1000)
        except Exception as exc:
            return False, int((time.monotonic() - start) * 1000)

    async def _probe_eventbus() -> tuple[bool, str, bool]:
        if not redis_url or not redis_url.strip():
            return True, "InMemory", False
        from app.agent_runtime.events.live_event_bus import LiveEventBusProbe
        try:
            bus = await asyncio.wait_for(
                LiveEventBusProbe.resolve_with_health_check(redis_url),
                timeout=postgres_timeout,
            )
        except asyncio.TimeoutError:
            return False, "InMemoryDegraded", True
        except Exception as exc:
            return False, "InMemoryDegraded", True
        return _eventbus_health(redis_url=redis_url, bus=bus)

    async def _probe_redis_inflight() -> tuple[bool, str]:
        """Phase 2.8B 新增 — 探测 Redis SET NX 写读。"""
        if not redis_inflight_url or not redis_inflight_url.strip():
            return True, "InMemory"  # 设计意图 = 进程级 in-memory
        try:
            import redis.asyncio as redis_async
            client = redis_async.from_url(redis_inflight_url, socket_connect_timeout=2.0, socket_timeout=2.0)
            try:
                await asyncio.wait_for(client.ping(), timeout=2.0)
            except Exception:
                return False, "InMemoryDegraded"
            try:
                probe_key = "__probe_inflight__"
                ok = await asyncio.wait_for(client.set(probe_key, "1", nx=True, ex=10), timeout=2.0)
                await client.delete(probe_key)
                return True, "Redis"
            except Exception:
                return False, "InMemoryDegraded"
            finally:
                try: await client.aclose()
                except Exception: pass
        except ImportError:
            return False, "InMemoryDegraded"

    # 并发探测
    async def _runner():
        return await asyncio.gather(_probe_postgres(), _probe_eventbus(), _probe_redis_inflight())

    try:
        (pg_ok, pg_latency), (eb_ok, eb_kind, _), (ri_ok, ri_backend) = await asyncio.wait_for(
            _runner(), timeout=overall_timeout
        )
    except asyncio.TimeoutError:
        pg_ok, pg_latency = False, 0
        eb_ok, eb_kind = False, "InMemoryDegraded"
        ri_ok, ri_backend = False, "InMemoryDegraded"

    warnings_list = []
    if not pg_ok:
        warnings_list.append("Postgres Checkpointer probe failed — production_dispatch_enabled will be forced OFF...")
    if not eb_ok:
        warnings_list.append("LiveEventBus Redis unavailable — degraded to InMemory...")
    if not ri_ok:
        warnings_list.append("Redis InFlight lock unavailable — degraded to in-memory...")

    ready = eb_ok  # Legacy 不依赖 Postgres;只要 LiveEventBus 健康就 ready=True
    prod_forced_off = not pg_ok  # 守禁令 #18:LangGraph 生产任务不得用纯 MemorySaver

    return ProbeReport(
        postgres_ok=pg_ok, postgres_url_echo=pg_url_echo, postgres_latency_ms=pg_latency,
        eventbus_kind=eb_kind, eventbus_url_echo=eb_url_echo, redis_ok=eb_ok,
        redis_inflight_ok=ri_ok, redis_inflight_url_echo=ri_url_echo,
        in_flight_lock_backend=ri_backend, in_flight_lock_ttl_seconds=redis_inflight_ttl_seconds,
        ready=ready, production_dispatch_forced_off=prod_forced_off,
        warnings=tuple(warnings_list),
    )
```

### 6.4 3 个失败语义

| 失败 | 决策 | 守禁令 |
|---|---|---|
| **Postgres 失败** | `production_dispatch_forced_off=True` | #18 |
| **LiveEventBus Redis 失败** | 降级 `InMemoryDegraded`（单 worker）| LiveEventBus 自处理 |
| **Redis InFlight 失败** | 降级 `InMemoryDegraded`（进程级 InFlightTaskRegistry）| #31（graceful degrade）|

**关键设计**：
- **`ready = eb_ok`**：Legacy 不依赖 Postgres，只要 LiveEventBus 健康就 ready=True
- **`production_dispatch_forced_off = not pg_ok`**：LangGraph 生产路径单独判断
- **`overall_timeout=8s`**：绝不阻塞启动（守 #28）

---

## 7. RedisInFlightRegistry（`redis_inflight_registry.py` 251 行）

### 7.1 设计要点

```python
"""Phase 2.8B 跨 worker 并行调度守护 — Redis SET NX + TTL。

设计要点(对应 docs/30 §3 + ADR-2.8B-1/4/8):

* **互斥锁** = ``Redis SET NX EX <ttl>``(原子写)。同一 ``task_public_id`` 的
  第二个 acquire 返回 ``False`` → 抛 ``ParallelDispatchGuardError``。
* **Owner 校验** = value 写 ``InflightOwner`` JSON;``release`` 时校验
  ``worker_id`` 一致才 DEL,防误删(老 worker 释放新 worker 的锁)。
* **TTL 兜底** = 锁默认 30 分钟过期;worker 崩溃后其他 worker 接管无需
  人工清理。``ttl_seconds`` 可调。
* **Graceful degrade** = Redis 不可用(``redis_client=None`` / SET 抛异常
  / ping 失败)时 ``try_acquire`` 返回 ``None``,``release`` 返回 ``False``;
  ``ApiDispatcher`` 调用方应 fallback 到进程级 ``InFlightTaskRegistry`` +
  WARN 日志(守禁令 #31)。

守禁令映射:
* #20 双引擎并行 →  ``ParallelDispatchGuardError`` 跨进程版本
* #31 Redis 不可用不 raise 5xx → try_acquire 返回 None,release 返回 False
* #32 owner 校验失败不阻断 → release 仅 WARN,不 raise
"""
```

### 7.2 `InflightOwner` 数据类

```python
@dataclass(frozen=True)
class InflightOwner:
    """持有者身份 — ``release`` 时用于 owner 校验。

    ``worker_id`` 默认格式 ``f"{HOSTNAME}-{pid}-{uuid4().hex[:6]}"``。
    """

    worker_id: str
    engine: str
    acquired_at: float


def _gen_worker_id() -> str:
    """生成 worker_id — 默认 ``f"{HOSTNAME}-{pid}-{uuid4().hex[:6]}"``。"""
    hostname = os.getenv("HOSTNAME", "unknown")
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:6]
    return f"{hostname}-{pid}-{suffix}"
```

### 7.3 `try_acquire()` — SET NX + owner 写

```python
async def try_acquire(
    self, task_public_id: str, engine: str
) -> Optional[InflightOwner]:
    """尝试跨 worker 获取锁。

    Returns:
        ``InflightOwner`` 当成功获得锁;
        ``None`` 当 Redis 不可用(调用方应 fallback to in-memory);

    Raises:
        ``ParallelDispatchGuardError`` 当同 task_public_id 已存在锁。
    """
    if self._redis is None:
        return None

    owner = InflightOwner(
        worker_id=self._worker_id,
        engine=engine,
        acquired_at=self._clock(),
    )
    key = self._key(task_public_id)
    payload = json.dumps(owner.__dict__)
    try:
        acquired = await self._redis.set(key, payload, nx=True, ex=self._ttl)
    except Exception as exc:
        logger.warning("RedisInFlightRegistry.try_acquire: Redis SET failed (%s); degrade to in-memory", exc)
        return None  # 守 #31:Redis 不可用不 raise

    if not acquired:
        # 已被其他 worker 持有 — 读 owner 用于错误信息
        running_engine = "<unknown>"
        try:
            existing = await self._redis.get(key)
            if existing:
                if isinstance(existing, bytes):
                    existing = existing.decode("utf-8")
                running_engine = json.loads(existing).get("engine", "<unknown>")
        except Exception as exc:
            logger.warning(...)

        from app.agent_runtime.dispatch_errors import ParallelDispatchGuardError
        raise ParallelDispatchGuardError(
            task_public_id=task_public_id,
            running_engine=running_engine,
            requested_engine=engine,
        )

    return owner
```

**关键**：SET NX EX 原子写 + 读现有 owner 用于错误信息。

### 7.4 `release()` — owner 校验防误删

```python
async def release(
    self, task_public_id: str, owner: Optional[InflightOwner]
) -> bool:
    """释放跨 worker 锁。校验 owner 防误删。

    Returns:
        ``True`` 当成功 DEL 自己的锁;
        ``False`` 当 Redis 不可用 / owner mismatch / key 已 TTL 过期;

    不抛 — TTL 兜底过期(守禁令 #32)。
    """
    if self._redis is None or owner is None:
        return False

    key = self._key(task_public_id)
    try:
        existing = await self._redis.get(key)
        if not existing:
            return False
        if isinstance(existing, bytes):
            existing = existing.decode("utf-8")
        cur = json.loads(existing)
        if cur.get("worker_id") != owner.worker_id:
            logger.warning(
                "RedisInFlightRegistry.release: owner mismatch task=%s holder=%s self=%s; TTL will reclaim",
                task_public_id, cur.get("worker_id"), owner.worker_id,
            )
            return False  # 守 #32:owner 校验失败不阻断
        await self._redis.delete(key)
        return True
    except Exception as exc:
        logger.warning("RedisInFlightRegistry.release: Redis DEL failed (%s); TTL will reclaim", exc)
        return False
```

**关键设计**：
- **owner 校验**：当前持有者 worker_id 必须匹配才能 DEL
- **mismatch 不 raise**：只 WARN + 返回 False（守 #32）
- **TTL 兜底**：过期自然清理

### 7.5 Key 命名

```python
REDIS_INFLIGHT_KEY_PREFIX = "inflight:"

def _key(self, task_public_id: str) -> str:
    return f"{self._prefix}{task_public_id}"
```

**Keyspace 隔离**：`inflight:` 前缀与 LiveEventBus（`agent_event:bus:`）和 DistributedCancellationService 隔离。

---

## 8. CrossWorkerRecovery（`cross_worker_recovery.py` 243 行）

### 8.1 设计要点

```python
"""Phase 2.8B 跨 worker checkpoint 恢复辅助 — 在 worker 启动期拉取 orphaned checkpoints。

设计要点(对应 docs/30 §3.4 + ADR-2.8B-8):

* **何时调用**:worker 进程启动时(``main.py`` lifespan 末尾)由运维显式 opt-in;
  不强制每次启动都跑(避免冷启动开销 + 不必要的 Postgres 读)。
* **做什么**:
  1. 读 ``checkpoints`` 表拿到最近 N 个 ``thread_id``(默认 50)
  2. 对每个 ``thread_id``,尝试 ``cp.get(config)`` 拿到最新 ``checkpoint`` dict
  3. 如果 ``checkpoint.metadata.source == "interrupted"`` 且 ``thread_id`` 当前
     **不在 Redis InFlight 锁里**(说明上次 dispatch 的 worker 已死),
     把 ``thread_id`` 加入"可恢复列表"
  4. 返回该列表 + 统计 dict
* **不做什么**:
  * **不**自动 resume — resume 是 ApiDispatcher.dispatch_resume 的责任
  * **不**DEL Redis lock — 由 TTL 自动过期兜底;若强制释放需运维介入
  * **不**写任何 MySQL/Postgres 表
* **守禁令映射**:
  * 守禁令 #18 — 不会修改 Postgres 表
  * 守禁令 #32 — release 只在运维显式调用时发生;TTL 兜底
  * 守禁令 #33 — graceful degrade(verify 失败 → 返回空 list + WARN)

**env-gate**:
- 默认行为 = dry-run,只返回 report,不做任何 mutation
- 通过 ``dry_run=False`` 可强制 DEL Redis lock(2.8B 范围默认 False)
"""
```

### 8.2 `RecoveryReport` 数据类

```python
@dataclass(frozen=True)
class RecoveryReport:
    """跨 worker 恢复扫描报告。"""

    scanned_thread_count: int
    recoverable_thread_ids: List[str] = field(default_factory=list)
    occupied_thread_ids: List[str] = field(default_factory=list)
    metadata_mismatch_thread_ids: List[str] = field(default_factory=list)
    redis_lock_skipped: bool = False
```

### 8.3 `scan_recoverable_checkpoints()` — 4 阶段扫描

```python
async def scan_recoverable_checkpoints(
    *,
    cp: Any,
    redis_inflight: Optional[Any],
    max_threads: int = 50,
    interrupted_marker: str = "interrupted",
) -> RecoveryReport:
    """扫描 Postgres checkpointer,找到可被本 worker 接管的 thread_id 列表。"""
    # 1. 列出最近 checkpoints（langgraph-checkpoint-postgres 2.0.25 list API）
    candidates: List[tuple[str, Optional[dict]]] = []
    try:
        count = 0
        async for cp_tuple in cp.list({}):
            try:
                thread_id = cp_tuple.config["configurable"]["thread_id"]
            except (KeyError, TypeError):
                continue
            candidates.append((str(thread_id), cp_tuple.checkpoint))
            count += 1
            if count >= max_threads:
                break
    except AttributeError:
        candidates = []  # list API 不存在 → 兜底空 list
    except Exception as exc:
        candidates = []

    # 2. 过滤:checkpoint.metadata.source == interrupted_marker
    recoverable = []
    mismatch = []
    for thread_id, checkpoint in candidates:
        if checkpoint is None:
            mismatch.append(thread_id)
            continue
        meta = checkpoint.get("metadata") or {}
        source = meta.get("source") or meta.get("langgraph_node") or ""
        if source != interrupted_marker:
            mismatch.append(thread_id)
            continue

        # 3. 检查 Redis 锁是否被占用
        if redis_inflight is None:
            recoverable.append(thread_id)
        else:
            try:
                owner = await redis_inflight.try_acquire(thread_id, "langgraph")
                if owner is None:
                    # Redis 不可用 → graceful degrade 为"可恢复"
                    recoverable.append(thread_id)
                else:
                    await redis_inflight.release(thread_id, owner)
                    recoverable.append(thread_id)
            except Exception as exc:
                from app.agent_runtime.dispatch_errors import ParallelDispatchGuardError
                if isinstance(exc, ParallelDispatchGuardError):
                    # 其他 worker 在跑
                    ...
                else:
                    recoverable.append(thread_id)

    # 4. 二次过滤:recoverable 中哪些是被其他 worker 锁住的
    occupied = []
    final_recoverable = []
    if redis_inflight is not None:
        for thread_id in recoverable:
            try:
                owner = await redis_inflight.try_acquire(thread_id, "langgraph")
                if owner is None:
                    occupied.append(thread_id)
                    continue
                await redis_inflight.release(thread_id, owner)
                final_recoverable.append(thread_id)
            except Exception as exc:
                from app.agent_runtime.dispatch_errors import ParallelDispatchGuardError
                if isinstance(exc, ParallelDispatchGuardError):
                    occupied.append(thread_id)
                else:
                    final_recoverable.append(thread_id)
    else:
        final_recoverable = recoverable

    return RecoveryReport(
        scanned_thread_count=len(candidates),
        recoverable_thread_ids=final_recoverable,
        occupied_thread_ids=occupied,
        metadata_mismatch_thread_ids=mismatch,
        redis_lock_skipped=redis_inflight is None,
    )
```

### 8.4 `release_orphaned_lock()` — 运维 opt-in

```python
async def release_orphaned_lock(
    *,
    redis_inflight: Any,
    task_public_id: str,
) -> bool:
    """运维显式调用 — 释放 orphaned Redis InFlight 锁(其他 worker 已死)。

    **强制 DEL 不校验 owner**(无视 owner mismatch) — 守禁令 #32 警告:
    仅当运维已确认持有者 worker 已死亡时才能调用;否则会破坏运行中任务的守护。

    Returns:
        True 当 DEL 成功(或 key 本来就不存在 — 无害 no-op);
        False 当 Redis 不可用
    """
    if redis_inflight is None:
        return False
    if redis_inflight._redis is None:
        return False
    key = f"{redis_inflight.key_prefix}{task_public_id}"
    try:
        deleted = await redis_inflight._redis.delete(key)
        return True
    except Exception as exc:
        return False
```

---

## 9. PostgresIntegration（`postgres_integration.py` 191 行）

### 9.1 4 张核心表

```python
# 4 张核心表 — 由 ``langgraph-checkpoint-postgres`` 2.0.25 ``setup()`` 创建
EXPECTED_CORE_TABLES = frozenset({
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
})
```

### 9.2 `verify_postgres_4_tables()` — 独立 psycopg 验证

```python
async def verify_postgres_4_tables(dsn: str) -> List[str]:
    """验证 Postgres checkpointer 真实创建了 4 张核心表。

    直接用 psycopg 对规范化后的 DSN 建短连接查 ``pg_tables``。
    """
    if not dsn or not dsn.strip():
        raise RuntimeError("verify_postgres_4_tables: DSN 为空")

    normalized = normalize_postgres_conn_string(dsn.strip())
    dsn_echo = _mask_dsn(normalized)

    present = set()
    try:
        import psycopg
        async with await psycopg.AsyncConnection.connect(normalized, autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                rows = await cur.fetchall()
                for row in rows:
                    present.add(str(row[0]))
    except ImportError as exc:
        raise RuntimeError(f"verify_postgres_4_tables: psycopg 不可用({exc}); dsn={dsn_echo} ...") from exc
    except Exception as exc:
        raise RuntimeError(f"verify_postgres_4_tables: 查询失败({type(exc).__name__}: {str(exc)[:200]}); dsn={dsn_echo} ...") from exc

    missing = EXPECTED_CORE_TABLES - present
    if missing:
        raise RuntimeError(f"Postgres checkpoint table verify failed: missing={sorted(missing)} present={sorted(present)} dsn={dsn_echo}")

    logger.info("Postgres checkpoint table verify success | tables=%d/%d | %s | dsn=%s", ...)
    return sorted(EXPECTED_CORE_TABLES)
```

**关键设计**：
- **直接用 psycopg 独立查 `pg_tables`**（不依赖 `cp` 对象）
- **接收原始 DSN**（内部 normalize）
- **查询失败 → raise RuntimeError**（fail-closed，不 assume OK）
- **日志脱敏**（不泄露密码）

### 9.3 `roundtrip_checkpoint()`

```python
async def roundtrip_checkpoint(
    cp: Any, *, thread_id: str, payload: Any, checkpoint_ns: str = ""
) -> Optional[dict]:
    """把 ``payload`` 写入 checkpoint → 读回 → 返回读到的 checkpoint dict。"""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
    saved = await cp.put(
        config,
        checkpoint={"v": 1, "marker": payload},
        metadata={"source": "phase_2.8b_integration_test"},
        new_versions={"v": 1},
    )
    logger.info("roundtrip_checkpoint: wrote thread=%s saved_id=%s", thread_id, saved.get("configurable", {}).get("checkpoint_id"))

    latest = await cp.get(config)
    if latest is None:
        return None
    return {
        "config": latest.get("config"),
        "checkpoint": latest.get("checkpoint"),
        "metadata": latest.get("metadata"),
        "parent_config": latest.get("parent_config"),
    }


async def count_checkpoints(cp: Any, *, thread_id: str) -> int:
    """返回 ``thread_id`` 下存在的 checkpoint 数。"""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    count = 0
    try:
        async for _ in cp.list(config):
            count += 1
    except AttributeError:
        latest = await cp.get(config)
        return 1 if latest is not None else 0
    return count
```

---

## 10. BusinessBudgets（`business_budgets.py` 202 行）

### 10.1 11 路径矩阵

```python
"""BusinessBudgets — Phase 2.8R-F 业务循环 Budget 控制。

实测路径矩阵(§6.4):
  A. 最短成功         ~ 12 super-step
  B. +prep 1 次       ~ 14
  C. prep 最大         ~ 17
  D. review 失败+repair ~ 18
  E. repair 最大       ~ 20
  F. format loss       ~ 22
  G. llm retry         ~ 24
  H. tool retry        ~ 26
  I. incremental       ~ 30+
  J. interrupt + resume ~ 28
  K. 恢复后继续         ~ 30

``recursion_limit`` = ``max_legal_observed`` + ``max(5, 20% safety margin)``
"""

PATH_MEASUREMENTS: dict[str, int] = {
    "A_shortest_success": 12,
    "B_prep_once": 14,
    "C_prep_max": 17,
    "D_review_fail_repair": 18,
    "E_repair_max": 20,
    "F_format_loss": 22,
    "G_llm_retry": 24,
    "H_tool_retry": 26,
    "I_incremental": 30,
    "J_interrupt_resume": 28,
    "K_resume_continue": 30,
}
```

### 10.2 `BusinessBudgets` 数据类

```python
@dataclass(frozen=True)
class BusinessBudgets:
    """统一配置各业务循环的预算。

    修改 default 不影响已有 checkpoint(只用 score 检查,不参与 hash)。
    """

    # 主循环 budget
    preparation_max: int = 2        # preparation_agent 节点最大重试
    review_max: int = 3             # review-regen 循环上限(MAX_REVIEW_LOOPS)
    format_max: int = 2             # format-check 循环上限(MAX_FORMAT_LOOPS)
    repair_max: int = 2             # repair subgraph 内部循环
    incremental_max: int = 5        # incremental 决策/执行循环

    # 工具 / LLM 重试 budget
    tool_retry_max: int = 3         # _run_tool_with_retry 最大次数
    llm_retry_max: int = 3          # LLMClient.generate_with_system retry

    # recursion_limit 加的安全余量(20%)
    safety_margin_percent: int = 20
    safety_margin_floor: int = 5    # 至少 5 步安全余量

    def max_legal_observed(self) -> int:
        """返回 PATH_MEASUREMENTS 中最大值(+1 fudge)。"""
        return max(PATH_MEASUREMENTS.values()) + 1

    def safe_margin(self) -> int:
        """安全余量 = max(5, max_legal * safety_margin_percent / 100)。"""
        legal = self.max_legal_observed()
        margin = max(self.safety_margin_floor, legal * self.safety_margin_percent // 100)
        return margin

    def compute_recursion_limit(self) -> int:
        """计算生产用 recursion_limit = max_legal + safe_margin。

        默认 30 + max(5, 6) = 30 + 6 = 36。后续调整实测值时一处改全应用。
        """
        return self.max_legal_observed() + self.safe_margin()
```

### 10.3 `validate_recursion_limit()`

```python
def validate_recursion_limit(value: int | None) -> int:
    """校验 recursion_limit 值是否安全。

    Raises:
        RecursionLimitConfigurationInvalidError: value < 已知最小合法值。
    """
    from app.core.exceptions import RecursionLimitConfigurationInvalidError

    default_budgets = get_default_budgets()
    min_safe = default_budgets.max_legal_observed()  # 无 margin 最低

    if value is None:
        return default_budgets.compute_recursion_limit()

    if not isinstance(value, int) or value <= 0:
        raise RecursionLimitConfigurationInvalidError(
            configured=value if isinstance(value, int) else -1,
            min_required=min_safe,
            detail={"reason": "must_be_positive_int", ...},
        )

    if value < min_safe:
        # Phase 2.8R-F 决策:低于 min_safe → 直接抛错,启动失败
        raise RecursionLimitConfigurationInvalidError(
            configured=value, min_required=min_safe,
            detail={"reason": "below_min_safe", ...},
        )

    return value
```

**关键**：
- **低于 min_safe → 启动失败**（fail-fast）
- **None → 用 budgets 计算的默认值**
- **env 覆盖校验**（RecursionLimitConfigurationInvalidError）

---

## 11. RecursionLimitConfig（`recursion_limit_config.py` 97 行）

### 11.1 `resolve_recursion_limit()` — env override

```python
"""RecursionLimitConfig — Phase 2.8R-F env-driven override。

设计要点(对应 docs/35 §6.3):
  * 默认值由 ``BusinessBudgets.compute_recursion_limit()`` 计算(37 步)。
  * 环境变量 ``AGENT_RUNTIME_RECURSION_LIMIT`` 可覆盖;非正整数 / 低于
    min_safe → 启动期 fail-fast(``RecursionLimitConfigurationInvalidError``)。
  * ``from_env_or_default()`` 单调用入口,生产代码应走这个。
"""


def resolve_recursion_limit(
    budgets: BusinessBudgets | None = None,
    *,
    env_var: str = "AGENT_RUNTIME_RECURSION_LIMIT",
) -> int:
    """从 env 读覆盖值,否则用 budgets 计算的默认值。

    Args:
        budgets: 业务预算(默认 singleton)
        env_var: env 变量名

    Returns:
        校验后的正整数。

    Raises:
        RecursionLimitConfigurationInvalidError: env 值无效
    """
    raw = os.environ.get(env_var)
    b = budgets or get_default_budgets()

    if raw is None:
        computed = b.compute_recursion_limit()
        logger.info("RecursionLimitConfig: %s=auto → recursion_limit=%d", env_var, computed)
        return computed

    try:
        parsed = int(raw.strip())
    except (TypeError, ValueError) as exc:
        from app.core.exceptions import RecursionLimitConfigurationInvalidError
        raise RecursionLimitConfigurationInvalidError(
            configured=-1,
            min_required=b.compute_recursion_limit(),
            detail={"reason": "env_value_not_int", ...},
        ) from exc

    validated = validate_recursion_limit(parsed)
    logger.info("RecursionLimitConfig: %s=%d → recursion_limit=%d", env_var, parsed, validated)
    return validated
```

**关键设计**：
- env 未设 → 用 budgets 计算（默认 36 步）
- env 非整数 → 抛错（fail-fast）
- env 值 < min_safe → 抛错
- env 值 ≥ min_safe → 接受

---

## 12. CheckpointWriter（`checkpoint_writer.py` 65 行）

```python
"""CheckpointWriter —— 节点完成后的双写 hook。

MemorySaver + ContextStore 双轨(Phase 2.1 §7):
* MemorySaver:LangGraph checkpointer,每个节点的状态 delta;测试用。
* ContextStore:Legacy SQL hook,``agent_tasks.current_node/resume_node/status``。
  API 路径仍从这张表读 status 决定能否接受 confirm。

Phase 2.1 范围内:
* ``write_checkpoint_after_node`` 是 thin async helper。
* 调用方(coordinator / dispatcher / 等)传 ``ContextStore`` 实例。
* 失败仅 log,不抛(规则:DB hiccup 永远不能挂 graph 节点)。
"""


async def write_checkpoint_after_node(
    *,
    task_id: str,
    current_node: str,
    resume_node: Optional[str],
    status: str,
    context_store_factory: Callable[[], Any],
) -> None:
    """节点完成后写 ContextStore,失败仅 log。"""
    if not task_id:
        return
    try:
        store = context_store_factory()
        await store.save_checkpoint(
            task_id=task_id,
            context=None,  # Phase 2.1 不镜像 AgentContext;Phase 2.2 接 SQL 时补
            node_name=current_node,
            resume_node=resume_node,
            status=status,
        )
    except Exception:
        logger.warning("write_checkpoint_after_node: ContextStore write failed (swallowed) task_id=%s current_node=%s", task_id, current_node, exc_info=True)
```

---

## 13. Alembic 迁移（19 个）

### 13.1 关键迁移（按 Phase）

| 迁移 | Phase | 作用 |
|---|---|---|
| `465ca149a559_init_database_schema_with_bigint.py` | Phase 1 | 初始 schema |
| `7a1b3c4d5e6f_add_agent_task_engine_fields.py` | Phase 2.8 | agent_tasks.engine_type / canary fields |
| `525edff4e293_ce_001_foundation.py` | Phase 2.8 + CE | CE 基础表 |
| `1a5ca1cbf787_add_context_snapshots_table_and_.py` | CE-04 | context_snapshots |
| `1b640574c715_ce_004_compression.py` | CE-04 | compression |
| `91fcfdb0376e_ce_003_memory.py` | CE-03 | memory |
| `a8b9c0d1e2f3_agent_events_add_production_fields.py` | Phase 2.8R | agent_events production fields |
| `b2_8r_b_create_execution_requests.py` | Phase 2.8R-B | agent_execution_requests Outbox |
| `b2_8r_e_idempotency.py` | Phase 2.8R-E | idempotency_key UNIQUE |
| `b9c0d1e2f3a4_create_agent_runs_table.py` | Phase 2.8 | agent_runs |
| `b3c2e4f5a1d0_add_conversation_context_tables.py` | Phase 2.8 | conversation_context |
| `4d22c4955510_add_idx_agent_events_task_created.py` | Performance | index 优化 |
| `2_9a26_*.py` | Phase 2.9A.26 | message_sequence / assistant_*.feedback / generations |

### 13.2 4 张 Postgres Checkpoint 核心表（由 setup() 自动创建）

| 表 | 用途 |
|---|---|
| `checkpoints` | LangGraph checkpoint 主表（thread_id + checkpoint_id + parent_checkpoint_id）|
| `checkpoint_blobs` | 大字段 blob（存储 channel_values）|
| `checkpoint_writes` | pending writes 暂存 |
| `checkpoint_migrations` | 迁移版本追踪 |

---

## 14. 测试与验证

### 14.1 测试目录

```
backend/tests/agent_runtime/persistence/
├── test_postgres_checkpointer.py        # 工厂 + FIX-B Pool + Windows 兼容
├── test_checkpointer_factory.py         # build_checkpointer 统一入口
├── test_probe_router.py                  # 3 类并发探测
├── test_redis_inflight_registry.py      # SET NX + owner 校验
├── test_cross_worker_recovery.py         # scan_recoverable + release_orphaned
├── test_postgres_integration.py          # verify_4_tables + roundtrip + count
├── test_checkpoint_writer.py             # 双写 hook
└── test_business_budgets.py             # 11 路径 + recursion_limit

backend/tests/
├── test_checkpointer_state_migration.py # 状态迁移
└── test_agent_execution_worker.py        # 多 worker Outbox + SKIP LOCKED
```

### 14.2 关键验证命令

```bash
# Checkpointer 工厂
cd backend && python -m pytest tests/agent_runtime/persistence/test_postgres_checkpointer.py -x -q
cd backend && python -m pytest tests/agent_runtime/persistence/test_checkpointer_factory.py -x -q

# ProbeRouter
cd backend && python -m pytest tests/agent_runtime/persistence/test_probe_router.py -x -q

# RedisInFlight + CrossWorker
cd backend && python -m pytest tests/agent_runtime/persistence/test_redis_inflight_registry.py -x -q
cd backend && python -m pytest tests/agent_runtime/persistence/test_cross_worker_recovery.py -x -q

# 实证 + Worker
cd backend && python -m pytest tests/agent_runtime/persistence/test_postgres_integration.py -x -q
cd backend && python -m pytest tests/agent_runtime/persistence/test_business_budgets.py -x -q
cd backend && python -m pytest tests/test_agent_execution_worker.py -x -q
```

### 14.3 端到端验证

```bash
# 启动 Postgres + Redis
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=xxx postgres:15
docker run -d -p 6379:6379 redis:7

# 设置 env
export AGENT_RUNTIME_CHECKPOINTER_BACKEND=postgres
export AGENT_RUNTIME_POSTGRES_URL=postgresql+psycopg://test:test@localhost:5432/test
export AGENT_RUNTIME_REDIS_URL=redis://localhost:6379/0
export AGENT_RUNTIME_RECURSION_LIMIT=40

# 启动后端
python -m uvicorn app.main:app --reload

# 验证 lifespan banner:
#   ProbeReport: postgres_ok=True eventbus_kind=Redis redis_inflight_ok=True
#   langgraph_readiness=True checkpointer_type=AsyncPostgresSaver

# 触发测试方案生成
curl -X POST http://localhost:8000/api/v1/messages/send \
  -H "Content-Type: application/json" \
  -d '{"content":"生成测试方案"}'

# 验证 Postgres 4 张表已创建
psql -h localhost -U test -d test -c "\dt"
# 应看到: checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations
```

---

## 15. 关键架构不变量

> 改动前必须确认的不变量。

| # | 规则 | 证据 | 验证 |
|---|---|---|---|
| 1 | **Phase 2.8R-C 严格化**：Postgres 不可用 → 抛 `CheckpointerUnavailableError`（**不**静默 fallback MemorySaver）| `checkpointer_factory.py` L52-60 | `grep "CheckpointerUnavailableError"` |
| 2 | **守 #18**：Postgres 不可用 → `production_dispatch_forced_off=True` | `probe_router.py` L316 | `grep "production_dispatch_forced_off"` |
| 3 | **守 #21**：任何 LangGraph 任务不得 downgrade 到 MemorySaver | `checkpointer_factory.py` 模块注释 | – |
| 4 | **FIX-B AsyncConnectionPool** 替代单连接（防 idle_session_timeout）| `postgres_checkpointer.py` L192-241 | `grep "AsyncConnectionPool"` |
| 5 | Pool 关键参数：`min_size=1 / max_size=10 / max_idle=300s / max_lifetime=1800s` | `postgres_checkpointer.py` L212-223 | – |
| 6 | **`_pg_pool` 属性绑定**（aclose 时一并关闭）| `postgres_checkpointer.py` L240 | `grep "_pg_pool"` |
| 7 | `_pg_health_check` 回调（`SELECT 1` 验证）| `postgres_checkpointer.py` L203-210 | – |
| 8 | **normalize_postgres_conn_string**：`+psycopg`/`+asyncpg` → `postgresql` + `ssl=false` → `sslmode=disable` | `postgres_checkpointer.py` L32-64 | – |
| 9 | **Windows 兼容**：ProactorEventLoop → SelectorEventLoop | `postgres_checkpointer.py` L67-106 | `grep "WindowsSelectorEventLoopPolicy"` |
| 10 | **3 类并发探测**：Postgres + LiveEventBus + Redis InFlight | `probe_router.py` L269-278 | – |
| 11 | **`overall_timeout=8s`**：绝不阻塞启动（守 #28）| `probe_router.py` L284 | – |
| 12 | **`ready = eb_ok`**：Legacy 不依赖 Postgres | `probe_router.py` L314 | – |
| 13 | **RedisInFlight SET NX EX**（原子写 + TTL 默认 1800s = 30min）| `redis_inflight_registry.py` 模块注释 + L146 | `grep "SET NX EX"` |
| 14 | **owner 校验**（release 时 worker_id 必须匹配）| `redis_inflight_registry.py` L203-211 | `grep "worker_id != owner.worker_id"` |
| 15 | **守 #31 graceful degrade**：Redis 不可用 → `try_acquire` 返回 None / `release` 返回 False | `redis_inflight_registry.py` L147-153 | – |
| 16 | **守 #32 owner mismatch 不阻断**：只 WARN + 返回 False | `redis_inflight_registry.py` L207-211 | – |
| 17 | **Key 命名**：`inflight:<task_public_id>`（与 LiveEventBus 隔离）| `redis_inflight_registry.py` L44 | `grep "REDIS_INFLIGHT_KEY_PREFIX"` |
| 18 | **CrossWorkerRecovery 默认 dry-run**：不自动 resume | `cross_worker_recovery.py` 模块注释 | – |
| 19 | **release_orphaned_lock 强制 DEL 不校验 owner**（运维 opt-in）| `cross_worker_recovery.py` L200-236 | – |
| 20 | **BusinessBudgets 11 路径矩阵**（单一 source of truth）| `business_budgets.py` L39-51 | – |
| 21 | **recursion_limit = max_legal_observed + max(5, 20% margin)** | `business_budgets.py` L89-94 | – |
| 22 | **validate_recursion_limit**：< min_safe → 启动失败（fail-fast）| `business_budgets.py` L160-172 | – |
| 23 | **RecursionLimitConfig env override**：`AGENT_RUNTIME_RECURSION_LIMIT` | `recursion_limit_config.py` L28-77 | – |
| 24 | **verify_postgres_4_tables 独立 psycopg 验证**（不依赖 `cp` 对象）| `postgres_integration.py` L45-109 | – |
| 25 | **4 张核心表白名单**（setup() 自动创建）| `postgres_integration.py` L32-37 | – |
| 26 | **CheckpointWriter 失败仅 log**（DB hiccup 永远不能挂 graph 节点）| `checkpoint_writer.py` 模块注释 | – |

---

## 16. 当前限制

### 16.1 真实限制（dev_3.0）

1. **scan_recoverable_checkpoints 不自动 resume**（运维 opt-in）
2. **Checkpointer 工厂单 backend 切换**（不能同时 memory + postgres）
3. **RedisInFlightRegistry 单 Redis 实例**（不支持 Redis Cluster）
4. **PATH_MEASUREMENTS 是 production 经验值**（实测需 docker 环境）
5. **probe_at_startup 不重试**：lifespan 一次探测，失败就 fail
6. **CrossWorkerRecovery 双 acquire**（性能损耗，但保证 occupied 准确）
7. **build_postgres_checkpointer 失败抛 CheckpointerUnavailableError**（旧行为 None + log 已删除）
8. **Alembic 19 个迁移**：跨多 phase，回滚复杂
9. **4 张核心表由 setup() 创建**（不在 Alembic 范围）
10. **agent_execution_worker 595 行**：单文件偏大

### 16.2 后续规划

- **ProbeReport 增强**：增加 Retry 策略（lifespan 启动可重试）
- **CrossWorkerRecovery 自动 resume opt-in**（通过 env `CROSS_WORKER_AUTO_RESUME=true`）
- **Checkpointer 切换**（active-passive 模式）
- **PATH_MEASUREMENTS 实测**：部署后采集真实路径长度
- **RecursionLimitConfig 扩展**：支持各 BusinessBudgets 字段 env 覆盖
- **AgentExecutionWorker 重构**：拆分为多模块

---

## 17. 与其他文档的关系

| 文档 | 关系 |
|---|---|
| [docs_x/02 §1.5](../02_TestAgent_项目总体技术方案.md) | Checkpointer 与持久化作为已实现能力 |
| [docs_x/02 §16](../02_TestAgent_项目总体技术方案.md) | Checkpoint 与服务恢复 |
| [docs_x/12 测试方案生成主图](12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md) | LangGraph v3 主图 checkpointer 注入 |
| [docs_x/17 SSE + LiveEventBus](17_TestAgent_SSE与LiveEventBus_技术实现文档.md) | event-list 历史回放与 checkpoint 状态 |
| `docs/35_TestAgent_Phase2.8R-D_真实多Worker测试运行手册.md` | 真实多 worker 部署参考 |

---

## 18. 索引自检（dev_3.0）

- [x] backend/app/agent_runtime/persistence/ 7 文件 / 1,666 行（实际 `wc -l` 验证）
- [x] agent_runtime 配套（business_budgets 202 + recursion_limit_config 97 + checkpoint_state_migration 94 + event_publisher 211 = 604 行）
- [x] services/agent_execution_worker.py 595 行
- [x] alembic 19 个迁移
- [x] FIX-B AsyncConnectionPool 替代单连接
- [x] Phase 2.8R-C 严格化（CheckpointerUnavailableError 不静默 fallback）
- [x] Phase 2.8B 跨 worker InFlight + Recovery
- [x] ProbeRouter 3 类并发探测（Postgres + LiveEventBus + Redis InFlight）
- [x] ProbeReport 13 字段 + 4 决策
- [x] RedisInFlightRegistry SET NX + TTL + owner 校验
- [x] CrossWorkerRecovery 4 阶段扫描 + release_orphaned_lock
- [x] PostgresIntegration 4 表验证 + round-trip + count
- [x] BusinessBudgets 11 路径矩阵
- [x] RecursionLimitConfig env override
- [x] 26 条架构不变量
- [x] 当前限制 + 后续规划
- [x] 与 docs_x/02/12/17 文档关系清晰
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[docs_x/02 §16](../02_TestAgent_项目总体技术方案.md) + [docs_x/12 测试方案生成主图](12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)。**