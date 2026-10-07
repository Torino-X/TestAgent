# TestAgent 从 Git Clone 到启动完整教程

> **配套文档**
> - [20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) — 外部服务 Docker 部署
> - [21_TestAgent_env参数详解.md](21_TestAgent_env参数详解.md) — .env 参数详解
> - [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) — 常见问题 FAQ
> - [25_TestAgent_数据库迁移与初始化.md](25_TestAgent_数据库迁移与初始化.md) — 数据库迁移
> - [README.md](../../README.md) — 项目入口

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`
**预计时间**: 全流程 30-60 分钟（含依赖下载）

> 本文档是 **新开发人员入职第一课**。从 0 开始：拉代码 → 装环境 → 启动后端 → 启动前端 → 跑通第一个测试方案生成流程。包含端到端验证清单。
> 行号以当前 `dev_3.0` HEAD 为准。

---

## 1. 文档说明

### 1.1 你将完成的事

按本文档走完，你将：

- ✅ 拉取项目代码（dev_3.0 分支）
- ✅ 安装 Python 3.11+ 和 Node 20+
- ✅ 启动 5 个外部服务（MySQL/Postgres/Redis/ES/Qdrant）
- ✅ 配置 `.env` 和 `frontend/.env.local`
- ✅ 跑数据库迁移
- ✅ 启动后端（uvicorn :8000）
- ✅ 启动前端（Vite :5313）
- ✅ 浏览器登录、跑通第一个测试方案生成
- ✅ 跑后端 + 前端单测

### 1.2 预计时间

| 阶段 | 时间 | 难度 |
|---|---|---|
| 1. 前置准备 | 5-10 分钟 | ⭐ 简单 |
| 2. 拉取代码 | 2 分钟 | ⭐ 简单 |
| 3. 后端环境 | 5-10 分钟 | ⭐⭐ 中等 |
| 4. 启动外部服务 | 5-10 分钟 | ⭐⭐ 中等 |
| 5. 启动后端 | 2-5 分钟 | ⭐ 简单 |
| 6. 启动前端 | 5-10 分钟 | ⭐ 简单 |
| 7. 端到端验证 | 5-10 分钟 | ⭐ 简单 |
| **总计** | **30-60 分钟** | |

### 1.3 前置要求

| 资源 | 最低要求 | 推荐 |
|---|---|---|
| 操作系统 | macOS 13+ / Ubuntu 22.04 / Windows 11 (WSL2) | macOS 14+ / Ubuntu 24.04 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 20 GB | 50 GB |
| Python | 3.11+ | 3.12 |
| Node.js | 20+ | 22 LTS |
| Docker | 24+ | 最新 |
| Docker Compose | v2.0+ | 最新 |
| Git | 2.30+ | 最新 |
| IDE | VSCode + Python 插件 | Cursor / PyCharm |

---

## 2. 阶段 1：前置准备

### 2.1 安装 Python 3.11+

```bash
# macOS（推荐 pyenv）
brew install pyenv
pyenv install 3.12.4
pyenv global 3.12.4

# Ubuntu
sudo apt install python3.12 python3.12-venv python3.12-dev

# Windows WSL2
sudo apt install python3.12 python3.12-venv python3.12-dev
```

### 2.2 安装 Node.js 20+

```bash
# macOS（推荐 nvm）
brew install nvm
mkdir ~/.nvm
echo 'export NVM_DIR="$HOME/.nvm"' >> ~/.zshrc
echo '[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"' >> ~/.zshrc
source ~/.zshrc
nvm install 22
nvm use 22

# Ubuntu
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install nodejs
```

### 2.3 安装 Docker

```bash
# macOS / Windows：Docker Desktop
# https://www.docker.com/products/docker-desktop/

# Ubuntu
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 注销重登录让 group 生效
docker --version
docker compose version
```

### 2.4 验证

```bash
python3 --version     # Python 3.11.x 或 3.12.x
node --version        # v20.x 或 v22.x
docker --version      # Docker 24+
git --version         # git 2.30+
```

---

## 3. 阶段 2：拉取代码

### 3.1 克隆仓库

```bash
cd ~/code  # 或任意工作目录
git clone <your-repo-url> TestAgent
cd TestAgent
```

### 3.2 切换到 dev_3.0 分支

```bash
# 查看所有分支
git branch -a

# 切换到 dev_3.0
git checkout dev_3.0

# 拉取最新
git pull origin dev_3.0

# 验证
git log --oneline -5
git status
```

### 3.3 验证项目结构

```bash
ls -la
# 应看到：
# README.md  CLAUDE.md  HANDO.md  HANDOFF.md
# backend/  frontend/  docs_x/  scripts/  data/  docker/
# .env.test.example  .gitignore  docker-compose.dev.yml
# docker-compose.prod.yml  docker-compose.test.yml
```

### 3.4 阅读入口文档（5 分钟）

按顺序读：

1. [README.md](../../README.md) — 项目入口
2. [CLAUDE.md](../../CLAUDE.md) — 项目协作指引（人也能看布局速查）
3. [HANDO.md](../../HANDO.md) — 最新接手状态（2026-08-24）
4. [docs_x/01 项目技术方案证据索引](../01_TestAgent_项目技术方案证据索引.md) — 3 份总览文档索引

**关键认知**：
- 守 #1：生产用 Postgres checkpointer（不静默 fallback MemorySaver）
- 守 #2：默认引擎是 `langgraph`（Phase 2.8R-K）
- 守 #18：canary_percent=0 + default_engine=legacy（必须显式写在生产 .env）

---

## 4. 阶段 3：后端环境配置

### 4.1 创建 Python 虚拟环境

```bash
cd backend

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1

# 验证
which python  # 应在 .venv/bin/python
python --version
```

### 4.2 安装依赖

```bash
# 必须激活 venv 后执行
cd backend
pip install --upgrade pip
pip install -r requirements.txt
```

**预计下载时间**：3-5 分钟（包含 langgraph、psycopg、paddlepaddle 等大型包）

**关键依赖**（来自 `requirements.txt`）：
- `fastapi>=0.115.0` / `uvicorn[standard]>=0.32.0`
- `pydantic>=2.10.0` / `pydantic-settings>=2.6.0`
- `sqlalchemy>=2.0.36` / `alembic>=1.14.0`
- `pymysql>=1.1.1` / `aiomysql>=0.2.0`
- `langgraph==0.3.34` / `langchain-core==0.3.86`（**精确锁定**）
- `langgraph-checkpoint-postgres==2.0.25` / `psycopg[binary]>=3.0,<4.0`
- `redis>=5.0.0,<6.0.0`
- `paddleocr>=3.0,<4.0` / `paddlepaddle>=3.0,<3.3`
- `elasticsearch>=8.15.0,<9.0.0` / `qdrant-client>=1.13.0,<2.0.0`

### 4.3 验证安装

```bash
python -c "import fastapi, langgraph, sqlalchemy, psycopg, redis, paddleocr; print('All imports OK')"
# 应输出: All imports OK
```

### 4.4 配置后端 .env

```bash
# 回到项目根目录
cd ..

# 复制模板
cp .env.test.example backend/.env

# 编辑 backend/.env
# macOS / Linux
nano backend/.env
# 或 VSCode
code backend/.env
```

**关键修改**（[21 §3.1 DATABASE_URL](21_TestAgent_env参数详解.md#31-database_url)）：

```bash
# ── 业务库 MySQL（dev 连云）──
# 改为你自己的云 MySQL 地址
DATABASE_URL=mysql+pymysql://testagent:STRONG_PASS@your.cloud.mysql.host:3306/testagent?charset=utf8mb4

# ── LangGraph Checkpointer Postgres（dev 本机 docker）──
# 保留默认即可（启动 dev docker 容器时匹配）
AGENT_RUNTIME_POSTGRES_URL=postgresql+psycopg://testagent:testpgpass@localhost:5432/langgraph

# ── LiveEventBus + InFlight Redis（dev 本机 docker）──
# 保留默认即可
AGENT_RUNTIME_REDIS_URL=redis://localhost:6379/0

# ── LLM API Key（生产时由 SettingsService 注入）──
# 开发测试可填入测试 key；生产必须走 SettingsService
AGENT_RUNTIME_LANGGRAPH_ENABLED=true
```

**关键提醒**：
- LLM API Key 在生产**不**入 `.env.prod`，通过 SettingsService 注入（[21 §18 LLM API Key 获取指引](21_TestAgent_env参数详解.md#18-llm-api-key-获取指引)）
- 守 #18：`AGENT_RUNTIME_CANARY_PERCENT=0` + `AGENT_RUNTIME_DEFAULT_ENGINE=legacy` 必须显式写
- `.env` 已在 `.gitignore`（不会误提交）

---

## 5. 阶段 4：启动外部服务

### 5.1 开发环境：PG + Redis

```bash
# 创建 dev 容器环境变量
cat > .env.dev <<'EOF'
POSTGRES_PASSWORD=testpgpass
REDIS_PASSWORD=
EOF

# 启动容器
docker compose -f docker-compose.dev.yml up -d

# 验证
docker compose -f docker-compose.dev.yml ps
# NAME                  STATUS              PORTS
# testagent-pg-dev       Up (healthy)        0.0.0.0:5432->5432/tcp
# testagent-redis-dev     Up (healthy)        0.0.0.0:6379->5432/tcp

# 健康检查
docker exec testagent-pg-dev pg_isready -U testagent -d langgraph
# 返回: accepting connections
docker exec testagent-redis-dev redis-cli ping
# 返回: PONG
```

**关联**：[20 §4 开发环境部署](20_TestAgent_外部服务Docker部署教程.md)

### 5.2 测试环境（可选）

```bash
# 如果要跑 e2e 测试
bash scripts/phase2_8r_start_test_infra.sh
# 自动启动 MySQL:3307 / Postgres:5433 / Redis:6380

# 验证
docker exec testagent-mysql mysqladmin ping -h localhost -u root -ptestpass
docker exec testagent-postgres pg_isready -U test -d langgraph_test
docker exec testagent-redis redis-cli ping
```

### 5.3 验证云 MySQL 连通

```bash
# 用 dev_check 验证（需设 CLOUD_HOST）
CLOUD_HOST=your.cloud.mysql.host bash scripts/dev_check.sh
# 自动 ping + 端口检查 + Python 客户端测试
```

---

## 6. 阶段 5：数据库迁移

### 6.1 运行 alembic upgrade

```bash
cd backend

# 激活 venv（如果还没激活）
source .venv/bin/activate

# 查看当前 migration 状态
alembic current
# 输出: （空 — 第一次跑）

# 跑到最新
alembic upgrade head
# 输出: Running upgrade  -> 465ca149a559, init database schema
#       或 Running upgrade  -> 7a1b3c4d5e6f, add agent task engine fields
```

**预计时间**：5-10 秒

### 6.2 验证表已创建

```bash
# 查看表
alembic show current

# 或直接 SQL（云 MySQL）
mysql -h your.cloud.mysql.host -u testagent -p testagent -e "SHOW TABLES;"
# 应看到：users / conversations / messages / files / agent_tasks / ...
```

### 6.3 初始化数据

```bash
# 创建 admin 用户（如果需要）
python -c "
import asyncio
from app.db.session import async_session
from app.models.user import User
from app.services.auth_service import hash_password

async def main():
    async with async_session() as s:
        admin = User(
            email='admin@testagent.local',
            password_hash=hash_password('admin123456'),
            is_admin=True,
        )
        s.add(admin)
        await s.commit()
        print('Admin created')

asyncio.run(main())
"
```

**关联**：[25_TestAgent_数据库迁移与初始化.md](25_TestAgent_数据库迁移与初始化.md)

---

## 7. 阶段 6：启动后端

### 7.1 启动 uvicorn

```bash
cd backend
source .venv/bin/activate

# 方式 1：直接 uvicorn（推荐 dev）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 方式 2：项目脚本（处理 Windows SelectorEventLoop 等）
python scripts/run_dev.py
```

### 7.2 验证后端启动

启动后应看到日志（关键关键字）：

```log
INFO:     Started server process
INFO:     Waiting for application startup
[Phase 2.8A Postgres Checkpointer 已就绪]
[Phase 2.8A+2.8B+2.8C ApiDispatcher 已挂 singleton]
[Phase 2.6 LiveEventBus 就绪]
[Phase 2.8R-B AgentExecutionWorker 已就绪]
INFO:     Application startup complete
INFO:     Uvicorn running on http://0.0.0.0:8000
```

**关键日志**：
- `Phase 2.8A Postgres Checkpointer 已就绪`
- `Phase 2.8R-B AgentExecutionWorker 已就绪`
- `LiveEventBus 就绪`

**如果缺失任一**，参考 [23 §4 启动失败排查](23_TestAgent_常见问题FAQ与故障排查.md)

### 7.3 健康检查

```bash
# 基础健康
curl http://127.0.0.1:8000/health
# 返回: {"status":"ok"}

# DB 健康
curl http://127.0.0.1:8000/api/health/db
# 返回: {"db":"ok","postgres":"ok","redis":"ok"}

# API 文档
open http://127.0.0.1:8000/docs  # Swagger UI
open http://127.0.0.1:8000/redoc  # ReDoc
```

### 7.4 跑后端单测（验证环境）

```bash
cd backend
source .venv/bin/activate

# 跑单测
python -m pytest tests/ -x -q
# 或指定模块
python -m pytest tests/agent_runtime/test_plan/ -x -q
python -m pytest tests/tools/ -x -q
```

**关联**：[25 §5 跑数据库迁移 + 单测](25_TestAgent_数据库迁移与初始化.md)

---

## 8. 阶段 7：启动前端

### 8.1 安装 Node 依赖

```bash
cd frontend

# 安装依赖
npm install
# 预计 2-5 分钟
```

**关键依赖**（来自 `package.json`）：
- `vue@^3.5.13` / `vue-router@^4.5.0`
- `naive-ui@2.41.0` / `@vicons/ionicons5@^0.13.0`
- `pinia@^2.3.0` / `axios@^1.7.9`
- dev: `vite@^6.0.5` / `vitest@^2.1.8` / `vue-tsc@^2.2.0` / `typescript@^5.7.2`

### 8.2 配置前端 .env

```bash
cd frontend

# 检查现有 .env
ls -la .env*

# 创建 .env.local（Vite 优先级最高）
cat > .env.local <<'EOF'
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_BASE_URL=ws://localhost:8000
EOF
```

### 8.3 启动 Vite dev server

```bash
cd frontend

npm run dev
# 输出:
#   VITE v6.0.5  ready in 500 ms
#   ➜  Local:   http://localhost:5313/
#   ➜  Network: http://192.168.x.x:5313/
```

### 8.4 验证前端

```bash
# 浏览器打开
open http://localhost:5313
# 或
# http://localhost:5313
```

**应看到**：登录页 / 完整布局（基于 Stitch 设计稿）

### 8.5 跑前端单测

```bash
cd frontend

# 跑 vitest
npm run test
# 输出: 全部通过
```

---

## 9. 阶段 8：端到端验证（关键）

### 9.1 浏览器流程

1. 打开 http://localhost:5313
2. 点击"注册"或"登录"（用 admin / admin123456）
3. 看到会话工作台（空状态）
4. 上传一个 .docx 文件（任意 docx）
5. 输入："生成测试方案"
6. 看到 AgentRunCard 显示执行状态
7. 看到章节确认卡片（如果需要）
8. 看到最终生成结果 + 下载链接

**如果任何步骤失败**，参考 [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md)

### 9.2 浏览器 DevTools 检查

按 `F12` 打开 DevTools：

```log
# Console 应无 ERROR
# Network → WS 应看到 SSE event 流
# Network → /api/agent/tasks/.../events 应持续收到 event
```

### 9.3 后端日志检查

```bash
# uvicorn 输出应持续打印
[tool_started] tool_name=RequirementParserTool
[tool_started] tool_name=TemplateParserTool
[tool_started] tool_name=SectionSuggestionTool
[tool_started] tool_name=TestPlanGeneratorTool
[tool_started] tool_name=ResultReviewTool
[tool_started] tool_name=WordExportTool
[task_completed] task_id=task_xxx
```

---

## 10. 阶段 9：跑完整测试套件

### 10.1 后端测试

```bash
cd backend
source .venv/bin/activate

# 跑全部测试
python -m pytest tests/ -x -q

# 跑特定模块
python -m pytest tests/agent_runtime/test_plan/ -x -q
python -m pytest tests/agent_runtime/test_plan_preparation_agent/ -x -q
python -m pytest tests/agent_runtime/test_plan_repair_agent/ -x -q
python -m pytest tests/agent_runtime/test_plan_incremental_agent/ -x -q
python -m pytest tests/tools/ -x -q
python -m pytest tests/test_result_review_tool.py -x -q
python -m pytest tests/agent_runtime/persistence/ -x -q

# 跑 Tool Adapter + 自定义合同
python -m pytest tests/agent_runtime/test_test_agent_tool_adapter.py -x -q

# 跑 Narrative Composer
python -m pytest tests/agent_runtime/test_narrative_composer.py -x -q
```

### 10.2 前端测试

```bash
cd frontend

# 跑 vitest
npm run test

# 跑特定文件
npx vitest run src/composables/useTaskEventReducer.spec.ts
npx vitest run src/utils/conversationTimeline.spec.ts
```

### 10.3 端到端 e2e

```bash
# 启动测试基建
bash scripts/phase2_8r_start_test_infra.sh

# 跑 e2e
bash scripts/phase2_8r_run_e2e.sh

# 停机
bash scripts/phase2_8r_stop_test_infra.sh
```

---

## 11. 完整验证清单

### 11.1 后端启动验证

| 验证项 | 命令 | 期望 |
|---|---|---|
| uvicorn 启动 | `curl http://127.0.0.1:8000/health` | `{"status":"ok"}` |
| DB 健康 | `curl http://127.0.0.1:8000/api/health/db` | `{"db":"ok",...}` |
| Postgres Checkpointer | `curl http://127.0.0.1:8000/api/health/postgres` | `{"postgres":"ok"}` |
| Redis 健康 | `curl http://127.0.0.1:8000/api/health/redis` | `{"redis":"ok"}` |
| Swagger UI | `open http://127.0.0.1:8000/docs` | 看到 API 列表 |
| Alembic 表 | `alembic current` | 显示最新 revision |

### 11.2 前端启动验证

| 验证项 | 命令 | 期望 |
|---|---|---|
| Vite 启动 | `open http://localhost:5313` | 看到登录页 |
| 路由 | `open http://localhost:5313/chat` | 看到工作台 |
| API 连接 | 浏览器 F12 → Network | API 请求 200 |

### 11.3 端到端流程

| 验证项 | 操作 | 期望 |
|---|---|---|
| 登录 | 浏览器登录 | 进入工作台 |
| 上传文件 | 拖入 docx | 上传成功 |
| 触发任务 | 输入"生成测试方案" → Enter | AgentRunCard 显示 |
| 章节确认 | （如需）点 confirm | 继续 |
| 下载产物 | 任务完成后 | 看到下载链接 |

### 11.4 测试通过

| 验证项 | 命令 | 期望 |
|---|---|---|
| 后端单测 | `pytest tests/ -x -q` | 全过 |
| 前端单测 | `npm run test` | 全过 |
| E2E | `bash scripts/phase2_8r_run_e2e.sh` | 全过 |

---

## 12. 常见启动错误速查

### 12.1 后端启动失败

| 错误 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'fastapi'` | venv 没激活 / 依赖没装 | `source .venv/bin/activate` + `pip install -r requirements.txt` |
| `RuntimeError: Form data requires python-multipart` | python-multipart 没装 | `pip install python-multipart` |
| `psycopg.OperationalError: connection failed` | Postgres 没起 / url 错 | `docker compose -f docker-compose.dev.yml ps` + 改 url |
| `pymysql.err.OperationalError: Can't connect to MySQL` | 云 MySQL 连不上 | `dev_check.sh` 验证网络 + 改 DATABASE_URL |
| `ImportError: paddleocr` | paddle 没装好 | `pip install paddleocr paddlepaddle`（重装） |
| `RecursionLimitConfigurationInvalidError` | recursion_limit 太小 | 不设 AGENT_RUNTIME_RECURSION_LIMIT（让默认）|
| `CheckpointerUnavailableError` | Postgres 不可用 | 检查 docker + 改 url |
| `Address already in use` | 8000 端口被占 | `lsof -i:8000` 杀掉旧进程 |
| `ProactorEventLoop on Windows` | psycopg 不兼容 | 用 `python scripts/run_dev.py` |

**详细排查**：[23 §4 启动失败排查](23_TestAgent_常见问题FAQ与故障排查.md)

### 12.2 前端启动失败

| 错误 | 原因 | 解决 |
|---|---|---|
| `Cannot find module 'vue'` | npm install 没跑 | `npm install` |
| `EADDRINUSE :::5313` | 端口被占 | `lsof -i:5313` 杀掉旧进程 |
| `CORS error` | 后端 CORS 配置 | 检查 `backend/app/main.py` 的 CORS 中间件 |
| `Network Error` | 后端没起 | 先起后端 |

---

## 13. 停止

### 13.1 停止后端

```bash
# 方式 1：Ctrl+C
# 方式 2：杀进程
lsof -i:8000 | tail -1 | awk '{print $2}' | xargs kill
```

### 13.2 停止前端

```bash
# 方式 1：Ctrl+C
# 方式 2：杀进程
lsof -i:5313 | tail -1 | awk '{print $2}' | xargs kill
```

### 13.3 停止外部服务

```bash
# dev
docker compose -f docker-compose.dev.yml down

# test
bash scripts/phase2_8r_stop_test_infra.sh
# 或
docker compose -f docker-compose.test.yml down
```

### 13.4 完全清理（删数据卷）

```bash
# ⚠️ 危险：会删所有数据
docker compose -f docker-compose.dev.yml down -v
docker compose -f docker-compose.test.yml down -v
```

---

## 14. 下一步

完成端到端验证后，你可以：

1. **阅读技术文档**（按顺序）：
   - [docs_x/02 项目总体技术方案](../02_TestAgent_项目总体技术方案.md)
   - [docs_x/03 项目实现原理与源码导读](../03_TestAgent_项目实现原理与源码导读.md)
   - [docs_x/10-19 技术实现文档（10 份）](../)

2. **开始第一次任务**（从简单开始）：
   - 改一个 Tool 的日志文案
   - 修一个 TODO 注释
   - 写一个测试用例

3. **遇到问题时**：
   - [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) — 常见问题
   - [docs_x/01 证据索引](../01_TestAgent_项目技术方案证据索引.md) — 证据指针
   - [HANDO.md](../../HANDO.md) — 最新接手状态

---

## 15. 索引自检

- [x] 9 阶段完整流程（前置 → 拉代码 → 配置 → 启动外部 → 数据库 → 启动后端 → 启动前端 → 端到端 → 测试）
- [x] 每阶段含预计时间 + 难度
- [x] 关键命令实际可执行（`pip install -r requirements.txt` / `alembic upgrade head` / `uvicorn` / `npm install` / `npm run dev`）
- [x] 关键日志关键字（Phase 2.8A / 2.8R-B / LiveEventBus）
- [x] 完整验证清单（11.1-11.4 共 4 类）
- [x] 常见启动错误速查（9 类后端 + 4 类前端）
- [x] 停止 + 清理指南
- [x] 关键守禁令提醒（#1 / #2 / #18）
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) + [21_TestAgent_env参数详解.md](21_TestAgent_env参数详解.md) + [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md)。**