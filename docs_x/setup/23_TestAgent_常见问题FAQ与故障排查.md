# TestAgent 常见问题 FAQ 与故障排查

> **配套文档**
> - [20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) — 外部服务 Docker 部署
> - [21_TestAgent_env参数详解.md](21_TestAgent_env参数详解.md) — .env 参数详解
> - [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — 从 Git Clone 到启动
> - [18_TestAgent_Checkpointer与持久化_技术实现文档.md](18_TestAgent_Checkpointer与持久化_技术实现文档.md) — Checkpointer 故障
> - [17_TestAgent_SSE与LiveEventBus_技术实现文档.md](17_TestAgent_SSE与LiveEventBus_技术实现文档.md) — SSE 故障
> - [HANDO.md](../../HANDO.md) — 最新接手状态

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`

> 本文档覆盖 **启动报错 / LLM 失败 / SSE 故障 / 性能问题 / 错误日志分析 / 服务降级** 的完整排查路径，含 40+ 个常见问题与解决方案。

---

## 1. 文档说明

### 1.1 适用场景

| 场景 | 何时查本 FAQ |
|---|---|
| 第一次启动失败 | 阶段 1-3 启动报错 |
| 启动后服务异常 | 启动成功但健康检查失败 |
| 端到端流程卡住 | 上传文件 / 生成 / 下载某个环节卡住 |
| LLM 调用失败 | 工具调用返回错误 / narrative 不出来 |
| SSE 不通 | 浏览器看不到事件流 |
| 性能问题 | 任务跑得很慢 / 内存爆 |
| 日志分析 | 看到 `FALLBACK_USED` / `WARN` / `ERROR` 不知道含义 |

### 1.2 错误码速查

| 范围 | 含义 | 类别 |
|---|---|---|
| **40001-40099** | 请求参数错误 | 客户端 |
| **40101-40199** | 认证 / Token | 客户端 |
| **40301-40399** | 权限 | 客户端 |
| **40401-40499** | 资源不存在 | 客户端 |
| **40901-40999** | 冲突 | 客户端 |
| **50001-50099** | 服务器内部错误 | 服务端 |
| **50101-50199** | 未实现 | 占位 |
| **50401-50499** | Engine dispatcher / 执行 | Phase 2.8R |
| **50501-50599** | LangGraph runtime / graph | Phase 2.8R |
| **50601-50699** | Checkpointer / Thread | Phase 2.8R |
| **50701-50799** | Artifact / Event idempotency | Phase 2.8R |
| **50801-50899** | Budget / recursion | Phase 2.8R |

---

## 2. 后端启动失败（9 类）

### 2.1 `ModuleNotFoundError: No module named 'fastapi'`

**症状**：
```
ModuleNotFoundError: No module named 'fastapi'
```

**原因**：Python 虚拟环境没激活 / 依赖没装

**解决**：
```bash
cd backend
source .venv/bin/activate  # macOS/Linux
# 或 .venv\Scripts\Activate.ps1（Windows）

pip install -r requirements.txt

# 验证
python -c "import fastapi; print(fastapi.__version__)"
```

### 2.2 `RuntimeError: Form data requires python-multipart`

**症状**：
```
RuntimeError: Form data requires "python-multipart" to be installed.
```

**原因**：上传文件功能需要 python-multipart

**解决**：
```bash
pip install python-multipart
# 验证
python -c "import multipart; print('OK')"
```

### 2.3 `psycopg.OperationalError: connection to server failed`

**症状**：
```
psycopg.OperationalError: connection to server at "localhost" (127.0.0.1), port 5432 failed: Connection refused
```

**原因**：Postgres 容器没起 / url 错 / 端口被占用

**解决**：
```bash
# 1) 检查 dev 容器
docker compose -f docker-compose.dev.yml ps
# 应看到 testagent-pg-dev Up

# 2) 检查 url（backend/.env）
grep AGENT_RUNTIME_POSTGRES_URL backend/.env
# 应是：postgresql+psycopg://testagent:testpgpass@localhost:5432/langgraph

# 3) 端口检查
lsof -i:5432
# 应看到 postgres 容器

# 4) 直接进容器测
docker exec testagent-pg-dev pg_isready -U testagent -d langgraph
# 返回：accepting connections
```

**关联**：[20 §4 开发环境部署](20_TestAgent_外部服务Docker部署教程.md)

### 2.4 `pymysql.err.OperationalError: (2003, "Can't connect to MySQL server")`

**症状**：
```
pymysql.err.OperationalError: (2003, "Can't connect to MySQL server on 'your.cloud.mysql.host' ([Errno 11003] getaddrinfo failed)")
```

**原因**：云 MySQL 连不上

**解决**：
```bash
# 1) 用 dev_check 验证
CLOUD_HOST=your.cloud.mysql.host bash scripts/dev_check.sh

# 2) 检查 .env 的 DATABASE_URL
grep DATABASE_URL backend/.env
# 修正：
DATABASE_URL=mysql+pymysql://USER:PASS@HOST:3306/DB?charset=utf8mb4

# 3) 直接 telnet 测试
telnet your.cloud.mysql.host 3306
```

### 2.5 `ImportError: paddleocr` / paddle 加载失败

**症状**：
```
ImportError: PaddleOCR was not found
```

**原因**：paddlepaddle 没装好

**解决**：
```bash
# 重装 paddle
pip install paddlepaddle>=3.0,<3.3 paddleocr>=3.0,<4.0

# macOS ARM64 注意
pip install paddlepaddle -i https://pypi.tuna.tsinghua.edu.cn/simple

# 验证
python -c "from paddleocr import PaddleOCR; print('OK')"
```

### 2.6 `RecursionLimitConfigurationInvalidError`

**症状**：
```
RecursionLimitConfigurationInvalidError: configured=10, min_required=30
```

**原因**：`AGENT_RUNTIME_RECURSION_LIMIT` 太小（< 30）

**解决**：
```bash
# 方案 1：删除环境变量（让默认 36）
unset AGENT_RUNTIME_RECURSION_LIMIT
# 或从 .env 删掉

# 方案 2：设到合理值
echo "AGENT_RUNTIME_RECURSION_LIMIT=40" >> backend/.env

# 验证
python -c "
from app.agent_runtime.recursion_limit_config import resolve_recursion_limit
print(resolve_recursion_limit())
"
# 应输出 36 或 40
```

**关联**：[18 §11 RecursionLimitConfig](18_TestAgent_Checkpointer与持久化_技术实现文档.md)

### 2.7 `CheckpointerUnavailableError: probe_unhealthy`

**症状**：
```
CheckpointerUnavailableError: reason="probe_unhealthy"
```

**原因**：Postgres Checkpointer 不可用（连接失败 / pool 满 / healthcheck 失败）

**解决**：
```bash
# 1) 直接用 psql 测连通
psql "postgresql+psycopg://testagent:testpgpass@localhost:5432/langgraph" -c "SELECT 1;"

# 2) 检查 Postgres 日志
docker logs testagent-pg-dev --tail 20

# 3) 看 uvicorn 启动时的 probe 错误
grep "ProbeRouter" backend.log
# 应有详细 reason
```

### 2.8 `RuntimeError: Form data requires python-multipart` 同 2.2

### 2.9 `OSError: [Errno 48] Address already in use`

**症状**：
```
OSError: [Errno 48] Address already in use
```

**原因**：8000 端口被占

**解决**：
```bash
# macOS / Linux
lsof -i:8000
# 看 PID
kill <PID>

# Windows
netstat -ano | findstr :8000
taskkill /F /PID <PID>

# 或改 uvicorn 端口（不推荐）
uvicorn app.main:app --port 8001
```

### 2.10 Windows `ProactorEventLoop` 错误

**症状**：
```
RuntimeError: Event loop is closed
或
RuntimeError: psycopg cannot run in ProactorEventLoop
```

**原因**：psycopg 不支持 Windows 默认 ProactorEventLoop

**解决**：
```bash
# 用项目脚本（已自动切换）
cd backend
python scripts/run_dev.py

# 或手动设
set EVENT_LOOP_POLICY=WindowsSelectorEventLoopPolicy
uvicorn app.main:app --reload
```

**项目已在 `main.py` 头部自动切换**（`sys.platform == "win32"`），但某些场景可能需要显式设。

---

## 3. 前端启动失败（4 类）

### 3.1 `Cannot find module 'vue'`

**症状**：
```
Error: Cannot find module 'vue'
```

**解决**：
```bash
cd frontend
rm -rf node_modules package-lock.json
npm install
```

### 3.2 `EADDRINUSE :::5313`

**症状**：
```
Error: EADDRINUSE: address already in use :::5313
```

**解决**：
```bash
# macOS / Linux
lsof -i:5313
kill <PID>

# Windows
netstat -ano | findstr :5313
taskkill /F /PID <PID>

# 或改端口（暂时）
npm run dev -- --port 5314
```

### 3.3 `Network Error` / `CORS error`

**症状**：
```
浏览器控制台:
Access to XMLHttpRequest at 'http://localhost:8000/api/...' from origin 'http://localhost:5313' has been blocked by CORS policy
```

**原因**：后端没起 / CORS 配置错

**解决**：
```bash
# 1) 确认后端在 8000
curl http://127.0.0.1:8000/health
# 返回 ok

# 2) 检查 frontend/.env.local
cat frontend/.env.local
# 应有：
#   VITE_API_BASE_URL=http://localhost:8000
#   VITE_WS_BASE_URL=ws://localhost:8000

# 3) 重启前端
cd frontend
# Ctrl+C 停
npm run dev
```

### 3.4 `TypeError: Cannot read properties of undefined`

**症状**：
```
TypeError: Cannot read properties of undefined (reading 'xxx')
```

**原因**：Naive UI / Pinia store 加载问题

**解决**：
```bash
cd frontend
rm -rf node_modules package-lock.json
npm install
npm run dev
```

---

## 4. 端到端流程卡住（5 类）

### 4.1 上传文件 500

**症状**：浏览器上传 .docx → 500 Internal Server Error

**排查**：
```bash
# 1) 看后端日志
tail -f backend.log | grep -E "upload|files/"
# 找具体错误

# 2) 检查文件大小
ls -lh /path/to/upload.docx
# 默认限制：30 MB

# 3) 检查文件类型
file /path/to/upload.docx
# 应是 Microsoft Word Document

# 4) 测直接 API
curl -X POST http://127.0.0.1:8000/api/v1/files/upload \
  -H "Authorization: Bearer <token>" \
  -F "file=@/path/to/test.docx"
```

### 4.2 任务卡在 "running" 不动

**症状**：任务创建后一直 `running`，无 tool_started 事件

**排查**：
```bash
# 1) 看后端 Worker 日志
grep "Phase 2.8R-B AgentExecutionWorker" backend.log
# 应有 "AgentExecutionWorker 已就绪"

# 2) 检查 SKIP LOCKED 是否工作
mysql -h ... -e "
SELECT id, status, created_at, started_at
FROM agent_tasks WHERE status='running' ORDER BY created_at DESC LIMIT 5;
"

# 3) 检查 SKIP LOCKED Outbox
mysql -h ... -e "
SELECT * FROM agent_execution_requests 
WHERE status IN ('queued','processing') 
ORDER BY created_at DESC LIMIT 5;
"

# 4) 看 uvicorn 进程是否在跑
ps aux | grep uvicorn
```

### 4.3 SSE 收不到事件

**症状**：浏览器 DevTools Network → WS 只有 0-1 个 event，然后断流

**排查**：
```bash
# 1) curl 直接测 SSE
curl -N http://127.0.0.1:8000/api/v1/agent/tasks/<task_id>/events
# 应看到持续的事件流

# 2) 检查 LiveEventBus
curl http://127.0.0.1:8000/api/health/redis
# 应 ok

# 3) 看 SSESlowConsumerGuard 日志
grep "slow consumer" backend.log
# 如果有 → 慢消费者被踢

# 4) 看 SequenceNumberAllocator
grep "SequenceNumberAllocator" backend.log
# 应有 INCR 调用
```

**关联**：[17 §7 SSESlowConsumerGuard](17_TestAgent_SSE与LiveEventBus_技术实现文档.md)

### 4.4 章节确认卡片不出现

**症状**：任务到 review_step 后不显示 section_confirm 卡片

**排查**：
```bash
# 1) 看 reducer 日志
grep "need_user_confirm" backend.log
# 应有 emit 记录

# 2) 看前端 reducer
grep "section_confirm" frontend/src/composables/useTaskEventReducer.ts
# 应有 case 'need_user_confirm'

# 3) 看 SSE 事件
curl -N http://127.0.0.1:8000/api/v1/agent/tasks/<id>/events | grep "need_user_confirm"
```

### 4.5 下载产物 404

**症状**：任务完成后点下载 → 404 Not Found

**排查**：
```bash
# 1) 看 artifact 表
mysql -h ... -e "
SELECT public_id, file_name, file_size, storage_path
FROM artifacts WHERE task_id=<id>;
"

# 2) 看文件系统
ls -la backend/data/artifacts/<id>/

# 3) 看 storage_path
docker exec testagent-backend ls -la /data/artifacts/
```

---

## 5. LLM 调用失败（6 类）

### 5.1 `LLM API key 错误`

**症状**：
```
openai.AuthenticationError: Incorrect API key provided
或
pydantic.ValidationError: 1 validation error for Settings
AGENT_RUNTIME_LLM_API_KEY
  Field required
```

**排查**：
```bash
# 1) 看 SettingsService 是否注入了 LLM config
mysql -h ... -e "SELECT * FROM model_configs WHERE user_id IS NULL;"

# 2) 测 LLM 连接
python backend/scripts/test_llm_connection.py

# 3) 看 SettingsService 日志
grep "SettingsService" backend.log
```

### 5.2 `LLM 调用超时`

**症状**：
```
openai.APITimeoutError: Request timed out
```

**排查**：
```bash
# 1) 检查网络（curl LLM base_url）
curl -I https://api.openai.com/v1/models
# 或自部署网关
curl -I https://your-llm-gateway.com/v1/models

# 2) 增加超时（修改配置 / 改 ENV）
EMBEDDING_TIMEOUT=60

# 3) 看 uvicorn 超时日志
grep "timeout" backend.log
```

### 5.3 `LLM rate limit`

**症状**：
```
openai.RateLimitError: Rate limit reached
```

**排查**：
```bash
# 1) 看 LLM Provider 配额（OpenAI dashboard / 公司网关）
# 2) 临时降级：增大 AgentRunTool concurrency 控制
# 3) 联系 LLM Provider
```

### 5.4 `LLM 返回空`

**症状**：LLM 返回 200 但 `choices` 为空

**排查**：
```bash
# 1) 看 raw response 日志
grep "raw_response" backend.log
# 2) 调 LLM Provider 验证
# 3) 看 result_parser 错误
grep "ResultParser\|ResultParseError" backend.log
```

### 5.5 `LLM 返回 JSON 格式错`

**症状**：
```
ResultParseError: missing required field 'sections'
```

**排查**：
```bash
# 1) 看 LLM 原始返回
grep "TestPlanGeneratorTool.*raw_json" backend.log
# 2) 验证 prompt schema
cat backend/app/common/prompt_builder.py | grep "schema"
```

### 5.6 `LLM streaming 中断`

**症状**：SSE 流中途断

**排查**：
```bash
# 1) 看 LLM Provider 日志
grep "stream.*abort\|stream.*close" backend.log
# 2) 测 LLM Provider 是否限流
# 3) 看 narrative_composer retry
grep "narrative.*retry" backend.log
```

---

## 6. SSE 与 LiveEventBus 故障（5 类）

### 6.1 SSE 连接立即断开

**症状**：浏览器 DevTools WS 状态：`pending` → `closed` 立即

**排查**：
```bash
# 1) curl 直接测
curl -N http://127.0.0.1:8000/api/v1/agent/tasks/<id>/events
# 应持续 stream

# 2) 看 backend 日志
grep "EventSource\|SSE\|stream" backend.log
# 应有 connected 信息

# 3) 检查 CORS（浏览器跨域）
# 浏览器 → Network → Headers → Access-Control-Allow-Origin
```

**关联**：[17 §4 HistoryDrainer](17_TestAgent_SSE与LiveEventBus_技术实现文档.md)

### 6.2 SSE 慢消费者

**症状**：日志 `slow consumer disconnected`

**排查**：
```bash
# 1) 看慢消费者日志
grep "slow consumer" backend.log
# 2) 检查 queue 大小
grep "max_queue" backend/app/agent_runtime/events/slow_consumer_guard.py
# 默认 max_queue=1000
# 3) 提高 queue
# 改 SSESlowConsumerGuard.__init__：max_queue=5000
```

### 6.3 SSE 事件丢失

**症状**：前端 timeline 缺事件

**排查**：
```bash
# 1) 看 SSE 丢失率
curl http://127.0.0.1:8000/api/agent/tasks/<id>/sse_loss
# 应接近 0

# 2) 看 sequence_no gap
mysql -h ... -e "
SELECT task_id, COUNT(*) n, MAX(sequence_no) max_seq, COUNT(*)/GREATEST(MAX(sequence_no), 1) coverage
FROM agent_events
WHERE task_id=<id>
GROUP BY task_id;
"
# coverage 应接近 1.0
```

**关联**：[17 §11 关键架构不变量 #25](17_TestAgent_SSE与LiveEventBus_技术实现文档.md)

### 6.4 LiveEventBus Redis 故障

**症状**：
```
WARNING: LiveEventBus: publish failed task_id=xxx; event still in MySQL
```

**排查**：
```bash
# 1) 测 Redis
docker exec testagent-redis-dev redis-cli ping

# 2) 看 probe_router 探测
grep "LiveEventBus\|redis_ok" backend.log

# 3) 检查 fallback
grep "eventbus_kind" backend.log
# 应显示 "InMemory"（degraded 模式）
```

### 6.5 SequenceNumberAllocator 失败

**症状**：
```
SequenceNumberAllocator: Redis INCR failed; DB fallback
```

**排查**：
```bash
# 1) Redis 连通
docker exec testagent-redis-dev redis-cli INCR seq:agent_events:1
# 应返回 1, 2, 3...

# 2) 看 DB fallback
mysql -h ... -e "
SELECT MAX(sequence_no) FROM agent_events WHERE task_id=1;
"
# 应单调递增
```

---

## 7. Checkpointer 与持久化故障（5 类）

### 7.1 Postgres 连接失败

**症状**：`CheckpointerUnavailableError: probe_failed`

**排查**：
```bash
# 1) psql 直接测
psql "postgresql+psycopg://..." -c "SELECT 1;"

# 2) 看 Postgres 日志
docker logs testagent-pg-dev --tail 20

# 3) 检查 url
grep AGENT_RUNTIME_POSTGRES_URL backend/.env
```

### 7.2 4 张核心表缺失

**症状**：`psycopg.errors.UndefinedTable: relation "checkpoints" does not exist`

**排查**：
```bash
# 1) 验证 4 张表
psql "postgresql+psycopg://..." -c "\dt"
# 应有 checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations

# 2) 手动建表
cd backend
python -c "
import asyncio
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def main():
    pool = AsyncConnectionPool('postgresql+psycopg://...', open=False)
    await pool.open()
    cp = AsyncPostgresSaver(conn=pool)
    await cp.setup()
    await pool.close()
    print('OK')

asyncio.run(main())
"
```

### 7.3 跨 worker 锁不释放

**症状**：任务卡 `running` 不动

**排查**：
```bash
# 1) 看 Redis InFlight 锁
docker exec testagent-redis-dev redis-cli KEYS "inflight:*"
# 应看到 task_xxx

# 2) 强制释放（运维 opt-in）
cd backend
python -c "
import asyncio
from app.agent_runtime.persistence.redis_inflight_registry import RedisInFlightRegistry
from app.core.config import get_settings

async def main():
    r = RedisInFlightRegistry(redis_client=...)
    await r.release_orphaned_lock(task_public_id='task_xxx')

asyncio.run(main())
"
```

### 7.4 Checkpoint 损坏

**症状**：`load_checkpoint failed`

**排查**：
```bash
# 1) 看 checkpoint 表
psql "postgresql+psycopg://..." -c "
SELECT thread_id, checkpoint_id, created_at
FROM checkpoints
WHERE thread_id='<id>'
ORDER BY created_at DESC LIMIT 5;
"

# 2) 用 verify_4_tables 验证
cd backend
python -m pytest tests/agent_runtime/persistence/test_postgres_integration.py -x -q
```

### 7.5 跨进程恢复失败

**症状**：Worker 死后任务不接续

**排查**：
```bash
# 1) 看 RecoveryReport
cd backend
python -c "
import asyncio
from app.agent_runtime.persistence.cross_worker_recovery import scan_recoverable_checkpoints
from app.agent_runtime.persistence.postgres_integration import verify_postgres_4_tables
from app.agent_runtime.persistence.redis_inflight_registry import RedisInFlightRegistry

async def main():
    cp = ...
    r = RedisInFlightRegistry(redis_client=...)
    report = await scan_recoverable_checkpoints(cp=cp, redis_inflight=r)
    print(report)

asyncio.run(main())
"
```

**关联**：[18 §8 CrossWorkerRecovery](18_TestAgent_Checkpointer与持久化_技术实现文档.md)

---

## 8. 性能问题（4 类）

### 8.1 任务跑得很慢

**排查**：
```bash
# 1) 看断点（哪个 tool 慢）
grep "tool_started\|tool_finished" backend.log | head -20
# 看每个 tool 的 duration_ms

# 2) 查 Postgres 慢 SQL
psql "postgresql+psycopg://..." -c "
SELECT query, calls, mean_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC LIMIT 10;
"

# 3) 看 LLM 调用延迟
grep "duration_ms" backend.log | sort -n | tail -20
```

### 8.2 内存爆

**排查**：
```bash
# 1) 看 Python 进程内存
ps aux | grep uvicorn | awk '{print $4, $11}'

# 2) 看 Redis 内存
docker exec testagent-redis-dev redis-cli INFO memory
# 默认 maxmemory 256mb + LRU

# 3) 看 Postgres 连接
psql "postgresql+psycopg://..." -c "
SELECT count(*), state FROM pg_stat_activity
WHERE application_name LIKE '%uvicorn%'
GROUP BY state;
"
```

### 8.3 大量 Tool 调用

**排查**：
```bash
# 1) 看 BusinessBudgets 是否超限
grep "BudgetExceeded\|max_steps" backend.log

# 2) 看 recursion_limit
grep "RecursionLimit\|GraphRecursion" backend.log
```

### 8.4 SSE 积压

**排查**：
```bash
# 1) 看 LiveEventBus queue
grep "queue full\|slow consumer" backend.log

# 2) 看 SSESlowConsumerGuard 触发
grep "dropping event" backend.log
```

---

## 9. 错误日志分析（关键模式）

### 9.1 `FALLBACK_USED` 模式

```log
FALLBACK_USED | component=context_store | from=db_lookup | to=memory_fallback | reason=xxx
```

**解读**：FALLBACK 链，每个 component 有不同 `from → to` 路径

| component | from | to | 原因 |
|---|---|---|---|
| `context_store` | db_lookup | memory_fallback | DB 不可用 |
| `intent_router` | llm_classify | keyword_match | LLM 失败 |
| `intent_router` | keyword_match | clarify_default | 关键词不命中 |
| `dynamic_agent.executor` | capability_handler | not_configured_result | handler 缺失 |
| `dynamic_agent.executor` | context_engine_analysis | deterministic_summary | CE 失败 |
| `postgres_checkpointer` | postgres_probe | memory_saver | PG 不可达 |

**这是项目标准日志格式**——按 component 维度追踪降级路径。

### 9.2 `BUDGET_EXCEEDED` 模式

```log
WARNING: BudgetExceeded: step_count=12, max_steps=8
```

**解读**：BusinessBudgets 超限，可能需要调整阈值（[18 §10 BusinessBudgets](18_TestAgent_Checkpointer与持久化_技术实现文档.md)）

### 9.3 `GraphRecursionError` 模式

```log
RecursionLimit of 25 reached without single task in 'agent'. Maximum 25 steps.
```

**解读**：LangGraph recursion 撞上限。提高 `AGENT_RUNTIME_RECURSION_LIMIT` 或调小 BusinessBudgets。

### 9.4 `ParallelDispatchGuardError` 模式

```log
ParallelDispatchGuardError: task_id=task_xxx already locked by engine=langgraph
```

**解读**：跨 worker 锁被占用。等 TTL 过期或 `release_orphaned_lock`。

### 9.5 `langgraph_readiness: False` 模式

```log
ProbeReport: postgres_ok=False, langgraph_readiness=False
```

**解读**：Postgres 不可用，LangGraph 任务被 ApiDispatcher 拒掉（守 #18）。

---

## 10. 服务降级模式

| 组件 | 降级目标 | 触发条件 | 用户感知 |
|---|---|---|---|
| **Postgres Checkpointer** | MemorySaver | `CheckpointerUnavailableError` | LangGraph 任务被拒（守 #18）|
| **LiveEventBus** | InMemoryLiveEventBus | Redis 不可用 | 跨 worker 事件丢失（单 worker 模式）|
| **Redis InFlight** | InMemory 进程级 | Redis SET NX 失败 | 多 worker 并发守护失效 |
| **Context Engine retrieval** | 不查（返回空）| ES / Qdrant 不可用 | 检索结果少 |
| **NarrativeComposer** | Deterministic Fallback | LLM 失败 / 超时 | 叙事为固定文案 |
| **RepairAgent** | legacy regen | 复杂失败 | 走 Phase 2.1 regen 路径 |
| **DynamicAgent** | legacy_simple_plan | LLM planner 失败 | 走确定性 2 步 plan |
| **ES / Qdrant** | 降级不查 | 缺库 | CE 检索无结果 |

**关键**：每个降级都有 `FALLBACK_USED` 日志，便于追溯。

---

## 11. 紧急情况处理（On-Call）

### 11.1 服务完全挂掉

```bash
# 1) 看所有容器状态
docker compose -f docker-compose.prod.yml ps

# 2) 看日志
bash scripts/prod_logs.sh backend --since 10m

# 3) 重启后端
docker compose -f docker-compose.prod.yml restart backend

# 4) 仍未恢复 → 全部重启
bash scripts/prod_stop.sh
bash scripts/prod_start.sh

# 5) 仍失败 → 回滚
bash scripts/prod_start.sh --rollback
# 或人工 git checkout
```

### 11.2 数据库连接池爆

```sql
-- MySQL
SELECT * FROM information_schema.processlist
WHERE db = 'testagent' AND COMMAND = 'Sleep'
ORDER BY TIME DESC LIMIT 20;
-- KILL <ID>;

-- Postgres
SELECT pid, state, query_start, query
FROM pg_stat_activity
WHERE datname = 'langgraph' AND state = 'idle'
ORDER BY query_start DESC LIMIT 20;
-- SELECT pg_terminate_backend(<pid>);
```

### 11.3 Redis 满了

```bash
docker exec testagent-redis redis-cli INFO memory
# 清理过期 key
docker exec testagent-redis redis-cli --scan --pattern "inflight:*" | xargs -r docker exec testagent-redis redis-cli DEL
```

### 11.4 LLM Provider 故障

```bash
# 1) 切 fallback
# backend/.env 加：
EMBEDDING_MODEL=fallback-model
# 重启后端

# 2) 等 Provider 恢复
# 3) 改回原 model
```

---

## 12. 索引自检

- [x] 9 大类错误（启动 9 + 前端 4 + e2e 5 + LLM 6 + SSE 5 + Checkpointer 5 + 性能 4 + 日志分析 5 + 降级 8）
- [x] 错误码速查（10 个范围）
- [x] 关键 FALLBACK_USED 模式表（6 个 component）
- [x] 紧急情况 on-call 流程
- [x] 降级模式表
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) + [HANDO.md](../../HANDO.md)。**