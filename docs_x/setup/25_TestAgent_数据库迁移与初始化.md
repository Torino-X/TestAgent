# TestAgent 数据库迁移与初始化

> **配套文档**
> - [20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) — 外部服务 Docker 部署
> - [21_TestAgent_env参数详解.md](21_TestAgent_env参数详解.md) — .env 参数详解
> - [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — 从 Git Clone 到启动
> - [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) — 常见问题 FAQ
> - [24_TestAgent_依赖与版本管理.md](24_TestAgent_依赖与版本管理.md) — 依赖与版本管理

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`

> 本文档覆盖 **alembic 基础命令、23 个迁移历史、初始数据脚本、ORM 模型、新表创建流程、回滚策略**。接手人必须知道如何跑迁移、回滚迁移、创建新迁移。
> 行号以当前 `dev_3.0` HEAD 为准。

---

## 1. 文档说明

### 1.1 适用读者

| 角色 | 何时查本文件 |
|---|---|
| **新开发人员** | 第一次跑迁移 / 初始化数据 |
| **SRE** | 生产部署跑迁移 / 回滚 |
| **Backend 开发者** | 加新表 / 改 schema 时 |

### 1.2 关键事实

| 项 | 事实 |
|---|---|
| 迁移工具 | **alembic 1.14+** |
| 迁移位置 | `backend/alembic/versions/` |
| 当前迁移数 | **23 个**（截至 2026-08-25）|
| ORM 模型数 | **22 个**（`backend/app/models/`）|
| 主业务库 | MySQL 8（DATABASE_URL）|
| LangGraph Checkpointer | PostgreSQL 16（AGENT_RUNTIME_POSTGRES_URL）|
| 初始数据脚本 | `backend/scripts/seed_dev_data.py` |

### 1.3 目录结构

```
backend/
├── alembic/
│   ├── env.py                          # alembic 环境配置
│   ├── script.py.mako                  # 模板
│   └── versions/                        # 23 个迁移文件
│       ├── 465ca149a559_init_database_schema_with_bigint.py
│       ├── 7a1b3c4d5e6f_add_agent_task_engine_fields.py
│       ├── ...
│       └── f020a1b2c3d4_add_image_understanding_configs_and_.py
├── app/
│   ├── models/                          # 22 个 ORM 模型
│   │   ├── __init__.py
│   │   ├── user.py / conversation.py / message.py
│   │   ├── agent_task.py / agent_run.py / agent_event.py
│   │   ├── artifact.py / tool_call.py
│   │   ├── context_engine.py
│   │   └── ...
│   └── db/session.py                    # SQLAlchemy async session
├── scripts/
│   ├── seed_dev_data.py                # 初始数据（admin / system_config）
│   ├── seed_user_model_config.py        # LLM config 注入
│   └── ...
└── alembic.ini                          # alembic 配置入口
```

---

## 2. Alembic 基础

### 2.1 配置文件（`backend/alembic.ini`）

```ini
[alembic]
script_location = backend/alembic
file_template = %%(year)d_%%(month).2d_%%(day).2d_%%(rev)s_%%(slug)s
prepend_sys_path = .
# sqlalchemy.url 从 backend/.env 读 DATABASE_URL
```

**关键点**：
- 迁移文件在 `backend/alembic/versions/`
- 文件名格式：`YYYY_MM_DD_<rev>_<slug>.py`
- `sqlalchemy.url` 在 `env.py` 里从环境变量读（**不**写死）

### 2.2 `env.py` 关键逻辑

```python
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# ── 从 .env 读 DATABASE_URL ──
import os
from dotenv import load_dotenv
load_dotenv(override=True)

config = context.config
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

# ── target_metadata ──
from app.db.base import Base
target_metadata = Base.metadata

# ── offline / online mode ──
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

**关键点**：
- `target_metadata = Base.metadata` → 自动检测模型变化
- 离线模式可用，但 dev / prod 都用 online

### 2.3 23 个迁移历史（按 Phase 分组）

| # | Revision | 描述 | Phase |
|---|---|---|---|
| 1 | `465ca149a559` | init database schema with bigint（users / conversations / messages / files / agent_tasks）| 1.0 |
| 2 | `70a7b52174b2` | add avatar_url to users | 1.0 |
| 3 | `b9c0d1e2f3a4` | create agent_runs table | 2.0 |
| 4 | `a8b9c0d1e2f3` | agent_events add production fields | 2.6 |
| 5 | `7a1b3c4d5e6f` | add agent_task engine fields | 2.7 |
| 6 | `b2_8r_b` | create execution_requests（Outbox）| 2.8R-B |
| 7 | `b2_8r_e` | idempotency | 2.8R-E |
| 8 | `c2_8r_e` | event_idemp_uniq | 2.8R-E |
| 9 | `4d22c4955510` | idx agent_events task_created | 2.8R |
| 10 | `b3c2e4f5a1d0` | conversation_context tables | 2.8 |
| 11 | `c4d5e6f7a8b9` | knowledge_configs table | 2.8 |
| 12 | `f020a1b2c3d4` | image_understanding_configs | 2.8 |
| 13 | `2_8r_merge_heads` | merge heads（2.8R 多分支合并）| 2.8R |
| 14 | `20260809` | attachment_understanding phase1 | 2.9A |
| 15 | `20260810` | request_understanding knowledge_mode | 2.9A |
| 16 | `525edff4e293` | ce_001 foundation | 2.9A.29 |
| 17 | `cd2c083cdad6` | ce_002 retrieval_audit | CE-02 |
| 18 | `91fcfdb0376e` | ce_003 memory | CE-03 |
| 19 | `1a5ca1cbf787` | add context_snapshots table | CE-04 |
| 20 | `1b640574c715` | ce_004 compression | CE-04 |
| 21 | `2_9a26_message_sequence` | message_sequence | 2.9A.26 |
| 22 | `2_9a26_assistant_feedbacks` | assistant_feedbacks | 2.9A.26 |
| 23 | `2_9a26_assistant_generations` | assistant_generations | 2.9A.26 |

---

## 3. 基础命令

### 3.1 查看当前状态

```bash
cd backend
source .venv/bin/activate

# 当前 migration 位置
alembic current
# 输出: 465ca149a559 (head) 或具体 revision

# 所有 migration 列表
alembic history --verbose
# 输出每个 migration 的 revision + 描述
```

### 3.2 跑迁移

```bash
cd backend
source .venv/bin/activate

# 跑全部
alembic upgrade head

# 跑一步
alembic upgrade +1

# 跑到指定 revision
alembic upgrade 7a1b3c4d5e6f

# 干跑（不执行，仅打印 SQL）
alembic upgrade head --sql
```

### 3.3 回滚

```bash
cd backend
source .venv/bin/activate

# 回滚一步
alembic downgrade -1

# 回滚到指定 revision
alembic downgrade 465ca149a559

# 全部回滚（清空）
alembic downgrade base
# ⚠️ 危险：删除所有表
```

### 3.4 创建新迁移

```bash
cd backend
source .venv/bin/activate

# 1) 改 ORM 模型（backend/app/models/xxx.py）
# 2) 自动生成迁移
alembic revision --autogenerate -m "add xxx table"

# 3) 检查生成的迁移文件
# backend/alembic/versions/<new_revision>_add_xxx_table.py
# 必看：upgrade() / downgrade() 是否完整

# 4) 手动调整（如有需要）
vim backend/alembic/versions/<new_revision>_add_xxx_table.py

# 5) 本地 dry-run
alembic upgrade head --sql

# 6) 跑迁移
alembic upgrade head

# 7) 验证表已创建
mysql -h ... -e "SHOW TABLES;" | grep xxx

# 8) 跑后端单测
pytest tests/ -x -q
```

### 3.5 强制标记

```bash
cd backend

# 当前 migration 已手工跑过，标记为已应用（不重新跑）
alembic stamp head

# 标记到指定 revision
alembic stamp 7a1b3c4d5e6f
```

### 3.6 完整命令速查

| 命令 | 作用 |
|---|---|
| `alembic current` | 查看当前 revision |
| `alembic history` | 查看所有 revision |
| `alembic upgrade head` | 跑全部 |
| `alembic upgrade +1` | 跑一步 |
| `alembic upgrade <rev>` | 跑到指定 |
| `alembic downgrade -1` | 回滚一步 |
| `alembic downgrade base` | 全部回滚（⚠️ 危险）|
| `alembic revision -m "msg"` | 手动创建空迁移 |
| `alembic revision --autogenerate -m "msg"` | 自动生成迁移 |
| `alembic stamp <rev>` | 强制标记 |
| `alembic upgrade head --sql` | 干跑（仅打印 SQL）|

---

## 4. 22 个 ORM 模型

### 4.1 业务核心（5 个）

| 模型 | 表 | 用途 |
|---|---|---|
| `User` | `users` | 用户（含 admin / 普通）|
| `Conversation` | `conversations` | 会话 |
| `Message` | `messages` | 消息（user / agent）|
| `File` (UploadedFile) | `uploaded_files` | 上传文件元数据 |
| `SystemConfig` | `system_configs` | 全局系统配置 |

### 4.2 Agent 核心（6 个）

| 模型 | 表 | 用途 |
|---|---|---|
| `AgentTask` | `agent_tasks` | 任务（含 engine_type / status / canary 字段）|
| `AgentRun` | `agent_runs` | 任务运行（Phase 2.6+）|
| `AgentEvent` | `agent_events` | 事件（SSE 持久化 + idempotency_key）|
| `AgentExecutionRequest` | `agent_execution_requests` | Outbox（多 worker 领取）|
| `ToolCall` | `tool_calls` | 工具调用 |
| `Artifact` | `artifacts` | 产物（word 文档等）|

### 4.3 确认 / 反馈（4 个）

| 模型 | 表 | 用途 |
|---|---|---|
| `HumanConfirmation` | `human_confirmations` | 章节确认 / format_loss 确认 |
| `MessageAttachment` | `message_attachments` | 消息附件关联 |
| `MessageFeedback` | `assistant_feedbacks` | 用户反馈（2.9A.26）|
| `MessageGeneration` | `assistant_generations` | LLM 生成历史（2.9A.26）|

### 4.4 Context Engine（3 个）

| 模型 | 表 | 用途 |
|---|---|---|
| `ContextSnapshot` | `context_snapshots` | CE 快照（CE-04） |
| `ContextEngineModel` | `context_*` | CE 内部数据 |
| `AgentContextSnapshot` | `agent_context_snapshots` | Agent + CE 关联 |

### 4.5 配置 / 知识 / 图像（3 个）

| 模型 | 表 | 用途 |
|---|---|---|
| `ModelConfig` | `model_configs` | LLM 模型配置（每用户）|
| `KnowledgeConfig` | `knowledge_configs` | MaaS 知识库配置 |
| `ImageUnderstandingConfig` | `image_understanding_configs` | 图像理解配置 |
| `AttachmentUnderstanding` | `attachment_understanding` | 文件理解元数据 |

### 4.6 摘要（1 个）

| 模型 | 表 | 用途 |
|---|---|---|
| `ConversationSummary` | `conversation_summaries` | 会话摘要（F016）|

---

## 5. 初始数据（seed scripts）

### 5.1 `seed_dev_data.py` — 默认 admin + system_config

```bash
cd backend
source .venv/bin/activate

# 跑（idempotent — 重复跑安全）
python scripts/seed_dev_data.py
# 输出:
#   admin user: skipped (已存在)
#   system_config: skipped (已存在)
#   或
#   admin user: created
#   system_config: created
```

**默认 admin 账户**：
- 用户名：`admin`
- 密码：`admin123456`
- 角色：`admin`
- public_id：`user_testagent_admin`
- display_name：`管理员`

**关键代码**：
```python
# 1) User 密码哈希
from app.core.security import hash_password
user = User(public_id="user_testagent_admin", password_hash=hash_password("admin123456"))

# 2) SystemConfig 默认值
config = SystemConfig(config_key="some_key", config_value="some_value")
```

**IDEMPOTENT 保护**：检查 `username == "admin"` 是否已存在，存在则 skip。

### 5.2 `seed_user_model_config.py` — 注入 LLM API Key

```bash
cd backend
source .venv/bin/activate

# API Key 必须从 env 读（不入 CLI 参数）
export SEED_LLM_API_KEY="sk-xxxxxxxxxxxxxx"

python -m scripts.seed_user_model_config \
  --username e2e_test \
  --api-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model-name qwen3.7-plus \
  --timeout 600
```

**关键点**：
- **API Key 只能从 env 读**（`SEED_LLM_API_KEY`），不入 CLI 参数（避免 shell history 泄露）
- 通过 `app.core.crypto.encrypt_api_key` 加密存储到 `model_configs` 表
- 后续 LangGraph 通过 `SettingsService` 解密注入

### 5.3 初始数据完整流程

```bash
# Step 1: 跑迁移
alembic upgrade head

# Step 2: 跑 seed_dev_data（admin + system_config）
python scripts/seed_dev_data.py

# Step 3: 注入 LLM config（按需）
export SEED_LLM_API_KEY="sk-xxx"
python -m scripts.seed_user_model_config --username admin \
  --api-base-url https://your-llm-gateway.com/v1 \
  --model-name gpt-4o

# Step 4: 验证
mysql -h ... -e "SELECT id, username, role FROM users;"
# 应看到 admin

mysql -h ... -e "SELECT * FROM model_configs WHERE user_id=1;"
# 应看到 LLM config
```

---

## 6. 创建新迁移（标准流程）

### 6.1 场景：在 `agent_tasks` 加 `priority` 字段

```bash
# Step 1: 改 ORM 模型
vim backend/app/models/agent_task.py
# 添加：
#   priority: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

# Step 2: 自动生成迁移
cd backend
source .venv/bin/activate
alembic revision --autogenerate -m "add priority to agent_tasks"
# 生成：<new_revision>_add_priority_to_agent_tasks.py

# Step 3: 检查生成的迁移
cat backend/alembic/versions/<new_revision>_add_priority_to_agent_tasks.py
# 应看到：
#   def upgrade():
#       op.add_column('agent_tasks', sa.Column('priority', sa.BigInteger(), ...))
#   def downgrade():
#       op.drop_column('agent_tasks', 'priority')

# Step 4: 干跑验证
alembic upgrade head --sql
# 输出: ALTER TABLE agent_tasks ADD COLUMN priority BIGINT ...

# Step 5: 实际跑
alembic upgrade head

# Step 6: 验证
mysql -h ... -e "DESCRIBE agent_tasks;" | grep priority
# 应看到: priority BIGINT NOT NULL DEFAULT 0

# Step 7: 跑单测
pytest tests/ -x -q

# Step 8: commit
git add backend/alembic/versions/<new_revision>_*.py
git commit -m "新增：agent_tasks 加 priority 字段"
# ⚠️ 守 #5: 中文 commit（与 CLAUDE.md 一致）
```

### 6.2 场景：新建表 `task_policies`

```bash
# Step 1: 写 ORM 模型
cat > backend/app/models/task_policy.py <<'EOF'
from sqlalchemy import BigInteger, String, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base

class TaskPolicy(Base):
    __tablename__ = "task_policies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"), nullable=False)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[...] = ...
EOF

# 记得在 backend/app/models/__init__.py 导入
echo "from app.models.task_policy import TaskPolicy" >> backend/app/models/__init__.py

# Step 2: 自动生成
alembic revision --autogenerate -m "add task_policies table"

# Step 3: 检查 + 调整
vim backend/alembic/versions/<new>*.py
# 必须包含：
#   op.create_table('task_policies', [...])
#   op.create_index('ix_task_policies_task_id', 'task_policies', ['task_id'])

# Step 4-8: 同上
```

### 6.3 场景：复杂 schema 变更

```python
# 1) 手动写迁移（不依赖 autogenerate）
alembic revision -m "split agent_events.payload column"
# vim 编辑生成的文件，手写 op.alter_column / op.add_column
```

**autogenerate 不能做的**：
- 数据迁移（data migration）
- 复杂索引（如复合索引 / 部分索引）
- 枚举值变更
- 表重命名（autogenerate 会 drop + create，丢数据）

### 6.4 迁移 checklist

```bash
# 1. ORM 模型改了吗？
diff main..feature -- backend/app/models/

# 2. autogenerate 完整吗？
# 检查生成的 .py 文件：upgrade() / downgrade() 都到位

# 3. 干跑 SQL 正确吗？
alembic upgrade head --sql

# 4. 实际跑成功吗？
alembic upgrade head

# 5. 单测通过吗？
pytest tests/ -x -q

# 6. 字段 / 表已存在？
mysql -h ... -e "DESCRIBE agent_tasks;" # 或 SHOW TABLES;

# 7. 回滚能成功吗？
alembic downgrade -1
alembic upgrade head  # 再跑回去

# 8. commit
git add backend/alembic/versions/ backend/app/models/
git commit -m "新增：xxx"
```

---

## 7. Postgres 端（LangGraph Checkpointer）

### 7.1 4 张核心表（langgraph-checkpoint-postgres 2.0.25 自动创建）

```sql
-- 由 cp.setup() 自动创建，不在 Alembic 范围
checkpoints
checkpoint_blobs
checkpoint_writes
checkpoint_migrations
```

**关键**：`langgraph-checkpoint-postgres` 包**自带建表逻辑**——`cp.setup()` 会自动建这 4 张表。**不**通过 Alembic 迁移管理。

### 7.2 验证 4 张表

```bash
# 用项目 helper
cd backend
python -c "
import asyncio
from app.agent_runtime.persistence.postgres_integration import verify_postgres_4_tables
print(asyncio.run(verify_postgres_4_tables('postgresql+psycopg://...')))
"
# 返回: ['checkpoint_blobs', 'checkpoint_migrations', 'checkpoints', 'checkpoint_writes']

# 或 psql
psql "postgresql+psycopg://..." -c "\dt"
# 应看到 4 张表 + pg_catalog / pg_toast 等系统表
```

### 7.3 手动 setup

```bash
cd backend

# 方式 1: pytest fixture 自动 setup
pytest tests/agent_runtime/persistence/ -x -q

# 方式 2: 手动代码
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

**关联**：[18 §9 PostgresIntegration](18_TestAgent_Checkpointer与持久化_技术实现文档.md)

---

## 8. 回滚策略

### 8.1 单步回滚

```bash
cd backend
alembic downgrade -1
# 验证
alembic current
```

### 8.2 回到指定 revision

```bash
cd backend
alembic downgrade 465ca149a559
# 验证
alembic current
```

### 8.3 全部回滚（⚠️ 危险）

```bash
cd backend
alembic downgrade base
# ⚠️ 删所有表，**不**能再 upgrade 回来（除非手写 SQL 恢复）
```

### 8.4 生产回滚

```bash
# 1) 停止 backend
docker compose -f docker-compose.prod.yml stop backend

# 2) 回滚 DB（手工）
mysqldump -h ... testagent > /tmp/backup-before-rollback.sql
alembic downgrade -1
# 验证
alembic current

# 3) 回滚后端代码
git checkout <previous-revision>
docker compose -f docker-compose.prod.yml up -d backend

# 4) 验证健康
curl http://127.0.0.1:8000/api/health/db
```

### 8.5 跨 Phase 迁移

```bash
# 例：从 2.8R 回滚到 2.7
# 1) 查迁移 history
alembic history
# 2) 找到 2.7 最后一个 revision
# 例: a8b9c0d1e2f3
# 3) 跨 Phase 回滚（可能涉及多个 revision）
alembic downgrade a8b9c0d1e2f3
# ⚠️ 警告：跨 Phase 回滚可能丢数据
```

---

## 9. 常见错误速查

### 9.1 `Target database is not up to date`

**症状**：
```
Target database is not up to date.
```

**解决**：
```bash
alembic upgrade head
```

### 9.2 `Can't locate revision identified by 'xxx'`

**症状**：
```
Can't locate revision identified by '465ca149a559'
```

**原因**：本地 `versions/` 目录不完整 / git 未拉全

**解决**：
```bash
git pull origin dev_3.0
ls backend/alembic/versions/ | wc -l  # 应是 23
```

### 9.3 `sqlalchemy.exc.OperationalError: (1146, "Table 'xxx' doesn't exist")`

**症状**：运行时报表不存在

**原因**：迁移未跑 / 跑一半

**解决**：
```bash
alembic current  # 看当前位置
alembic upgrade head
```

### 9.4 `Duplicate entry` / `Column already exists`

**症状**：迁移 idempotent 失败

**原因**：迁移不是 idempotent + 已经部分跑过

**解决**：
```bash
# 1) 看当前状态
alembic current
# 2) 如果不准确，手动标记
alembic stamp <next-revision>
# 3) 再跑
alembic upgrade head
```

### 9.5 autogenerate 没生成

**症状**：改了模型但 `alembic revision --autogenerate` 没生成

**原因**：
- `env.py` 没设 `target_metadata = Base.metadata`
- `Base` 类没继承 `declarative_base()`

**解决**：
```python
# 确认 env.py
target_metadata = Base.metadata  # ← 必须有

# 确认 Base
from sqlalchemy.orm import declarative_base
Base = declarative_base()  # ← 必须继承
```

### 9.6 Alembic 找不到 .env

**症状**：
```
sqlalchemy.exc.OperationalError: (2003, "Can't connect to MySQL")
```

**原因**：`alembic` 从 `backend/alembic/env.py` 跑，找 `.env` 路径错误

**解决**：
```bash
# 在 backend/ 目录跑
cd backend
source .venv/bin/activate
alembic current
# env.py 里有 load_dotenv(override=True) 从 ../.env 读
```

---

## 10. 索引自检

- [x] 23 个迁移历史（按 Phase 分组）
- [x] 22 个 ORM 模型（业务 / Agent / 反馈 / CE / 配置）
- [x] 6 大类基础命令（current / upgrade / downgrade / revision / stamp / --sql）
- [x] 初始数据脚本（seed_dev_data.py / seed_user_model_config.py）
- [x] 新建迁移 3 场景流程（加字段 / 新表 / 复杂变更）
- [x] Postgres 端 4 张核心表（langgraph-checkpoint-postgres 自动创建）
- [x] 回滚策略（单步 / 指定 / 全部 / 生产 / 跨 Phase）
- [x] 6 类常见错误速查
- [x] 完整迁移 checklist
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) + [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) + [24_TestAgent_依赖与版本管理.md](24_TestAgent_依赖与版本管理.md)。**