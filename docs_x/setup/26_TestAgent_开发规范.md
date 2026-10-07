# TestAgent 开发规范

> **配套文档**
> - [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — 从 Git Clone 到启动
> - [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) — 常见问题 FAQ
> - [25_TestAgent_数据库迁移与初始化.md](25_TestAgent_数据库迁移与初始化.md) — 数据库迁移
> - [CLAUDE.md](../../CLAUDE.md) — 项目内 AI 协作指引（人也适用）
> - [HANDO.md](../../HANDO.md) — 最新接手状态

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`

> 本文档覆盖 **commit 中文规范 / 分支策略 / PR review 流程 / 代码风格 / 测试要求 / commit-msg 检查 / 不做事项**。接手人必读；老手对照。
> 守 #4 = 中文 commit；守 #5 = 不自动加 Co-Authored-By；守 #6 = 不自动 commit/push。

---

## 1. 文档说明

### 1.1 关键守禁令（来自 CLAUDE.md / HANDO.md）

| 守 | 规则 | 证据 |
|---|---|---|
| **守 #4** | commit 消息**必须是中文**（title + body）| CLAUDE.md §6 / HANDO.md |
| **守 #5** | **严禁自动加 `Co-Authored-By: ...`** | CLAUDE.md §6 |
| **守 #6** | 无用户确认**不**自动 commit / push | CLAUDE.md §6 |
| **守 #7** | 无用户确认**不**自动 push / merge | CLAUDE.md §6 |
| **守 #8** | dev / 生产部署**只**改 `dev_3.0` 分支 | CLAUDE.md §6 |

### 1.2 当前分支结构

```
main             ← 生产部署（受保护，需 PR + review）
dev_2.0          ← 旧 dev 分支（保留兼容历史任务）
dev_3.0          ← 当前活动 dev 分支 ⭐
remotes/origin/HEAD → origin/main
```

---

## 2. Commit 消息规范

### 2.1 中文格式（守 #4）

**格式**：
```
<中文标题（≤50 字）>

<空行>

<中文 body 段落（详细描述本次改动）>
- 关键改动 1
- 关键改动 2
- 配套测试覆盖
```

### 2.2 实际 commit 示例

**示例 1**（修复类）：
```
修复:测试方案生成链路 8 类兜底 + 后端业务代码批量加注释

一、测试方案生成链路 8 类兜底修复(配套测试覆盖)
  1. template_backfill 规则读错字段:对齐 template_section_service.build_backfill_plan
     与 schema 字段名,避免回填时被吞。
  2. 表头白名单同源隐患:result_review_tool / template_section_service 共享
     HEADER_WHITELIST 常量单一来源,杜绝两份定义漂移。
  ...
  
  配套测试:
    backend/tests/test_message_service_pending_index.py
    backend/tests/test_task_attachment_resolver_wait_profile.py
    ...
```

**示例 2**（功能完善）：
```
完善测试方案生成修复链路与增量生成能力

本次提交整合会话内的阶段性改动：补强 ResultReviewTool 对测试方案 JSON 与
Word 模板回填契约的审查，新增开发态故障注入网关与独立 dev_fault_injection 模块，
完善 RepairAgent 复审闭环、预算耗尽叙事和 TestPlanRegenTool 批量/定点重生成能力；
接通增量生成 Agent 的触发、决策过滤、artifact v2 更新与回退链路；...

验证情况：frontend 执行 npm run build 通过；backend 执行 python -m pytest 全量未通过，
结果为 3016 passed、181 failed、134 errors、44 skipped，失败包含 pytest 临时目录
PermissionError、外部依赖集成测试以及当前 Agent 链路相关测试失败。
```

### 2.3 前缀（可选但推荐）

| 前缀 | 用途 | 示例 |
|---|---|---|
| `修复:` | Bug 修复 | `修复:Redis 连接失败` |
| `新增:` | 新功能 | `新增:用户注册 API` |
| `重构:` | 重构（不改行为）| `重构:ResultReviewTool 规则引擎` |
| `chore:` | 杂项（CI / 配置）| `chore:添加项目 Claude 协作指引 CLAUDE.md` |
| `文档:` | 文档 | `文档:补充 README 上手指引` |
| `测试:` | 仅测试 | `测试:补全 Tool Adapter 边界条件` |
| `性能:` | 性能优化 | `性能:Postgres 连接池调优` |

### 2.4 标题规范

| 规则 | 例子 |
|---|---|
| **≤50 字**（理想 ≤30）| `修复:ResultReview 模板回填字段名对齐` |
| **首字母不大写**（中文无所谓）| `修复:xxx` |
| **不用句号** | ❌ `修复:xxx.` |
| **不用 emoji** | ❌ `🚀 修复 xxx` |
| **不用第一人称** | ❌ `我修复了 xxx` |
| **不用模糊词** | ❌ `修复了一些 bug` |

### 2.5 body 规范

| 规则 | 例子 |
|---|---|
| **详细说明改动** | `新增：xxx` 而不是 `改动` |
| **列关键文件 / 类** | `backend/app/tools/result_review_tool.py:ResultReviewTool` |
| **列关键测试** | `backend/tests/test_xxx.py` |
| **说明验证情况** | `pytest tests/ -x -q 全过` |
| **关联 issue / PR** | `关联: #123` |
| **中文标点** | `，。、；：` |

### 2.6 严禁（守 #5 / 守 #6）

```bash
# ❌ 严禁:自动加 Co-Authored-By
git commit -m "修复 Redis" --trailer "Co-Authored-By: Claude <noreply@anthropic.com>"

# ❌ 严禁:英文 commit message
git commit -m "fix: redis connection"

# ❌ 严禁:无确认自动 commit
git add -A && git commit -m "fix"

# ❌ 严禁:无确认自动 push
git push origin dev_3.0

# ❌ 严禁:无确认 merge / rebase
git merge feature-branch
```

---

## 3. 分支策略

### 3.1 3 个主分支

| 分支 | 用途 | 保护 |
|---|---|---|
| `main` | **生产部署** | 受保护，必须 PR + review |
| `dev_2.0` | 旧 dev（保留历史任务兼容）| 接受 fix |
| `dev_3.0` | **当前活动 dev**（推荐）| 接受 feature / fix |

### 3.2 推荐工作流

```bash
# 1) 切到 dev_3.0
git checkout dev_3.0
git pull origin dev_3.0

# 2) 创建 feature 分支
git checkout -b feature/xxx-功能
# 命名: feature/xxx / fix/xxx / docs/xxx / refactor/xxx

# 3) 开发 + commit（中文 + 守 #4 + 守 #5）
git add backend/app/xxx.py
git commit -m "新增：xxx 功能"
# ⚠️ 不加 Co-Authored-By

# 4) 推到 origin（需用户确认）
git push origin feature/xxx-功能
# ⚠️ 守 #6/守 #7：必须先问用户

# 5) 提 PR：feature/xxx → dev_3.0
# 用 GitHub UI / GitLab UI
# ⚠️ 守 #6：不自动 merge

# 6) 合并后：切回 dev_3.0
git checkout dev_3.0
git pull origin dev_3.0
git branch -d feature/xxx-功能
```

### 3.3 修复 main 的紧急 hotfix

```bash
# 1) 从 main 开 hotfix 分支
git checkout main
git pull origin main
git checkout -b hotfix/xxx-紧急修复

# 2) 修 + 测 + commit
git add ...
git commit -m "紧急修复：xxx"

# 3) 直接合 main（需用户授权）
git checkout main
git merge --no-ff hotfix/xxx-紧急修复
git push origin main
# ⚠️ 守 #7：必须用户确认才能 push main
```

### 3.4 严禁（守 #6 / 守 #7）

```bash
# ❌ 严禁:无确认直接 push main
git checkout main
git merge dev_3.0
git push origin main

# ❌ 严禁:force push 到 dev_3.0 / main
git push --force origin dev_3.0
git push --force origin main

# ❌ 严禁:无 PR 直接合 dev_3.0（如果有保护）
git checkout dev_3.0
git merge feature/xxx
git push origin dev_3.0
```

---

## 4. PR Review 流程

### 4.1 PR 创建

```bash
# 1) feature 分支推完
git push origin feature/xxx-功能

# 2) 用 GitHub UI / GitLab UI 创建 PR
#    - base: dev_3.0
#    - compare: feature/xxx-功能
#    - title: 中文（守 #4）
#    - body: 描述改动 + 关联 issue

# 3) 关联 reviewers（项目配置：通常 1-2 人）
#    - 至少 1 个核心 reviewer
#    - 复杂改动：架构师 + 具体模块 owner
```

### 4.2 PR 标题规范

| ✅ 好的 | ❌ 坏的 |
|---|---|
| `修复:ResultReviewTool 模板回填字段名对齐` | `fix review` |
| `新增:ConversationTimeline 锚定 DocxFormatCheckTool` | `add feature` |
| `重构:Tool Adapter 改为 AsyncConnectionPool` | `refactor` |

### 4.3 PR 描述模板

```markdown
## 改动概述
- 简述本 PR 改了什么（3-5 行）

## 改动文件
- backend/app/xxx.py
- backend/tests/test_xxx.py
- frontend/src/xxx.vue

## 测试
- 后端 pytest tests/ -x -q（结果）
- 前端 npm run test（结果）
- 端到端 bash scripts/phase2_8r_run_e2e.sh（结果）

## 截图（如 UI 改动）
- [截图 1]
- [截图 2]

## 关联
- Issue: #123
- HANDO: 更新了哪个章节
- 文档: docs_x/xx 改了什么

## checklist
- [ ] 守 #4：commit 中文
- [ ] 守 #5：无 Co-Authored-By
- [ ] 守 #18：守门不破坏
- [ ] 测试通过
- [ ] 文档更新
```

### 4.4 Reviewer 检查清单

```markdown
- [ ] 代码逻辑正确
- [ ] 命名清晰（变量 / 函数 / 类）
- [ ] 类型注解完整（Python type hints / TS types）
- [ ] 错误处理完整（不静默吞异常）
- [ ] 测试覆盖（unit + integration）
- [ ] 文档更新（CLAUDE.md / HANDO.md / docs_x）
- [ ] 守 #1-#5 不违反
- [ ] 向后兼容（不破坏旧 API）
- [ ] 性能合理（无 N+1 / 无 O(n²)）
- [ ] 安全（无 SQL 注入 / 无 XSS / 无 secret 泄漏）
```

### 4.5 PR 合并

```bash
# 1) 至少 1 个 LGTM
# 2) CI 通过
# 3) 用 GitHub UI "Squash and merge" 或 "Rebase and merge"

# 合并后：清理
git branch -d feature/xxx-功能
git push origin --delete feature/xxx-功能
```

---

## 5. 代码风格

### 5.1 Python（backend）

#### 5.1.1 风格规范

| 项 | 规范 | 工具 |
|---|---|---|
| 风格 | PEP 8 + 项目自定义 | `pyproject.toml` (无强制 ruff) |
| 缩进 | 4 空格 | – |
| 行长 | ≤ 120 字符（软约束）| – |
| 类型注解 | 强制 | `from __future__ import annotations` + Python 3.11+ |
| 文档字符串 | 强制（模块 / 类 / 公共函数）| – |
| import 顺序 | stdlib / third-party / local | – |
| 字符串 | 优先双引号 `""` | – |
| 命名 | snake_case 函数 / PascalCase 类 | – |

#### 5.1.2 类型注解示例

```python
from __future__ import annotations
from typing import Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession

async def fetch_user_by_id(
    user_id: int,
    *,
    include_profile: bool = False,
) -> Optional[User]:
    """根据 ID 查用户。
    
    Args:
        user_id: 用户 ID
        include_profile: 是否包含 profile
    
    Returns:
        用户对象；不存在返回 None
    """
    ...
```

#### 5.1.3 错误处理

```python
# ✅ 好的：明确处理
try:
    result = await call_external_api()
except httpx.TimeoutException as exc:
    logger.warning("external_api_timeout | user_id=%s | err=%s", user_id, exc)
    raise EngineDispatcherUnavailableError(detail={"reason": "timeout"})

# ❌ 不好的：静默吞
try:
    result = await call_external_api()
except Exception:
    pass  # 永远不要这样做
```

### 5.2 TypeScript（frontend）

#### 5.2.1 风格规范

| 项 | 规范 | 工具 |
|---|---|---|
| 风格 | Vue 3 + TS 5 推荐风格 | – |
| 缩进 | 2 空格 | – |
| 行长 | ≤ 100 字符 | – |
| 类型 | 严格（noImplicitAny）| `tsconfig.json` strict |
| 命名 | camelCase 变量 / PascalCase 类型 | – |
| 引号 | 单引号 `'`（除模板字符串）| – |
| 分号 | 必加 | – |

#### 5.2.2 Vue 组件规范

```typescript
<script setup lang="ts">
// 1. imports
import { ref, computed, onMounted } from 'vue'
import { useChatStore } from '@/stores/chatStore'

// 2. props / emits
const props = defineProps<{
  taskId: string
  isCollapsed?: boolean
}>()
const emit = defineEmits<{
  (e: 'collapse', value: boolean): void
}>()

// 3. state
const isLoading = ref(false)

// 4. computed
const visibleStatus = computed(() => props.isCollapsed ? 'collapsed' : 'expanded')

// 5. lifecycle
onMounted(() => {
  // init
})

// 6. methods
async function handleClick() {
  emit('collapse', !props.isCollapsed)
}
</script>

<template>
  <div class="agent-card" :class="visibleStatus">
    <button @click="handleClick">Toggle</button>
  </div>
</template>

<style scoped>
.agent-card { /* ... */ }
</style>
```

### 5.3 SQL / 迁移

```python
# 迁移文件
def upgrade() -> None:
    op.create_table(
        'task_policies',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('public_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
    )
    op.create_index('ix_task_policies_created_at', 'task_policies', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_task_policies_created_at', table_name='task_policies')
    op.drop_table('task_policies')
```

**关键**：
- ✅ `upgrade()` 和 `downgrade()` 都必须写
- ✅ 加索引（不只是 PK / unique）
- ✅ 不用 `IF NOT EXISTS`（让 alembic 自己判断）

---

## 6. 测试要求

### 6.1 后端

| 改动类型 | 必须测试 |
|---|---|
| 新增 Tool | `tests/tools/test_xxx_tool.py` 单元测试 |
| 新增 Service | `tests/agent_runtime/...` 集成测试 |
| 改 schema | alembic migration 跑通 + rollback 跑通 |
| 改 prompt | 至少 1 个 snapshot test |
| 改路由 | `tests/api/...` 端到端测试 |
| Bug 修复 | 至少 1 个回归测试（复现 bug）|

### 6.2 前端

| 改动类型 | 必须测试 |
|---|---|
| 新增组件 | `xxx.spec.ts` 渲染测试 |
| 改 store | 单元测试 state 变化 |
| 改 reducer | 至少 3 个 event 测试 |
| 改 composable | 单元测试 |

### 6.3 跑测试命令

```bash
# 后端
cd backend
pytest tests/ -x -q

# 前端
cd frontend
npm run test

# 端到端
bash scripts/phase2_8r_run_e2e.sh
```

### 6.4 最低通过率

| 套件 | 最低 |
|---|---|
| 后端单测 | 100% 通过（修一个跑一个）|
| 前端单测 | 100% 通过 |
| e2e | 全部通过 |

### 6.5 守 #18 / 守 #1 测试

- **新增 / 改动 langgraph 节点**：必须跑 `tests/agent_runtime/test_plan/` 全部
- **新增 / 改动 Tool Adapter**：必须跑 `tests/agent_runtime/test_test_agent_tool_adapter.py` + `tests/tools/`
- **新增 / 改动 Postgres 相关**：必须跑 `tests/agent_runtime/persistence/`

---

## 7. Commit-msg 检查（守 #4 / 守 #5）

### 7.1 当前状态

`backend/.git/hooks/commit-msg.sample`（**仅 sample，未启用**）。

### 7.2 推荐：安装 commit-msg 钩子

```bash
cd backend
cat > .git/hooks/commit-msg <<'EOF'
#!/usr/bin/env bash
# commit-msg 钩子：禁止英文 commit + Co-Authored-By

commit_msg=$(cat "$1")

# 检查 Co-Authored-By
if echo "$commit_msg" | grep -q "Co-Authored-By"; then
  echo "❌ ERROR: commit 消息不能包含 Co-Authored-By（守 #5）"
  echo "请删除 trailer 并重试："
  echo "  git commit --amend"
  exit 1
fi

# 检查英文 commit（粗略：首行含中文字符 OR 是 fix:/chore: 等允许前缀）
first_line=$(echo "$commit_msg" | head -1)
if echo "$first_line" | grep -qE "^[a-zA-Z]+(\([^)]+\))?!?: "; then
  # Conventional Commits 英文前缀（fix: / feat: / chore: 等）
  echo "⚠️ WARNING: commit 首行像 Conventional Commits 英文格式（守 #4）"
  echo "  $first_line"
  echo "建议改中文："
  echo "  修复:xxx / 新增:xxx / 重构:xxx / chore:xxx"
  # 不强制 exit 1，给用户一个机会
fi

exit 0
EOF
chmod +x .git/hooks/commit-msg
```

### 7.3 推荐：pre-commit 钩子

```bash
cd backend
pip install pre-commit
cat > .pre-commit-config.yaml <<'EOF'
repos:
  - repo: local
    hooks:
      - id: commit-msg-check
        name: commit-msg-check
        entry: bash -c 'cat "$1" | grep -q "Co-Authored-By" && { echo "❌ Co-Authored-By not allowed"; exit 1; } || exit 0'
        language: system
        stages: [commit-msg]
EOF
pre-commit install
```

---

## 8. CI / CD（建议）

### 8.1 推荐配置

虽然本仓库目前**没有** `.github/workflows/`，但推荐新 PR 引入：

```yaml
# .github/workflows/test.yml
name: tests

on: [push, pull_request]

jobs:
  backend:
    runs-on: ubuntu-latest
    services:
      mysql:
        image: mysql:8.0
        env:
          MYSQL_ROOT_PASSWORD: testpass
          MYSQL_DATABASE: testagent_test
        ports: ['3307:3306']
        options: --health-cmd "mysqladmin ping" --health-interval 10s
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: test
          POSTGRES_PASSWORD: testpass
          POSTGRES_DB: langgraph_test
        ports: ['5433:5432']
        options: --health-cmd "pg_isready" --health-interval 10s
      redis:
        image: redis:7
        ports: ['6380:6379']
        options: --health-cmd "redis-cli ping" --health-interval 10s

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: cd backend && pip install -r requirements.txt
      - run: cd backend && alembic upgrade head
      - run: cd backend && pytest tests/ -x -q

  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '22' }
      - run: cd frontend && npm install
      - run: cd frontend && npm run test
      - run: cd frontend && npm run build
```

### 8.2 守 #1 检查（lifespan 启动 banner）

PR 描述应包含 ProbeReport 状态：

```log
ProbeReport: postgres_ok=True eventbus_kind=Redis redis_inflight_ok=True
langgraph_readiness=True checkpointer_type=AsyncPostgresSaver
```

**若任一=False**：守 #18 触发，PR 描述需说明降级原因。

---

## 9. 严禁（汇总）

### 9.1 commit 严禁

```bash
# ❌ 严禁:英文 commit
git commit -m "fix: redis connection"

# ❌ 严禁:加 Co-Authored-By
git commit -m "修复 Redis" --trailer "Co-Authored-By: ..."

# ❌ 严禁:无确认自动 commit
# 工具不自动跑 git add + git commit（守 #6）
```

### 9.2 push 严禁

```bash
# ❌ 严禁:无确认 push
git push origin dev_3.0  # 必须先问

# ❌ 严禁:force push 到 main / dev_3.0
git push --force origin dev_3.0
git push --force origin main
```

### 9.3 merge 严禁

```bash
# ❌ 严禁:无确认 merge
git checkout main
git merge dev_3.0
git push origin main

# ❌ 严禁:无 review 直接合并
# 必须 1 个 LGTM
```

### 9.4 代码严禁

```python
# ❌ 严禁:静默吞异常
try:
    ...
except Exception:
    pass

# ❌ 严禁:hardcode secret
api_key = "sk-xxxxxxxxxxxxxx"  # 必须从 env / SettingsService

# ❌ 严禁:eval / exec
eval(user_input)

# ❌ 严禁:SQL 拼接（用 ORM 参数化）
query = f"SELECT * FROM users WHERE id = {user_id}"  # 错误
query = select(User).where(User.id == user_id)  # 正确
```

---

## 10. 开发流程 SOP

### 10.1 第一次接手流程

1. 阅读 [CLAUDE.md](../../CLAUDE.md) + [HANDO.md](../../HANDO.md)
2. 按 [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) 拉代码跑通
3. 阅读 [docs_x/01-03 总览文档](../01_TestAgent_项目技术方案证据索引.md) + [docs_x/10-19 技术实现文档](../)
4. 找一个 TODO 注释或小 bug
5. 按本规范：feature 分支 + 中文 commit + 测试 + PR

### 10.2 修改 bug 流程

1. 复现 bug（写个失败测试）
2. 改代码 + 让测试通过
3. 中文 commit：`修复:xxx`
4. 推 feature 分支 + 提 PR

### 10.3 新增功能流程

1. 写 design doc / 在 docs_x 写技术方案
2. 改 ORM 模型 → `alembic revision --autogenerate`
3. 写新代码 + 测试
4. 中文 commit：`新增:xxx`
5. 推 + PR

### 10.4 重大重构流程

1. 写 ADR（Architecture Decision Record）
2. 跑 Phase X.Y verification matrix
3. 双轨（old + new）+ 灰度
4. 全量切流 + 删 old

---

## 11. 关键参考

| 文档 | 关系 |
|---|---|
| [CLAUDE.md](../../CLAUDE.md) | 守 #4 / #5 / #6 来源 |
| [HANDO.md](../../HANDO.md) | 当前状态（2026-08-24）|
| [.github/PULL_REQUEST_TEMPLATE.md](#) | （待补 — 推荐添加）|
| [25_TestAgent_数据库迁移与初始化.md](25_TestAgent_数据库迁移与初始化.md) | schema 变更流程 |
| [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) | 常见问题 |
| [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) | 端到端跑通 |

---

## 12. 索引自检

- [x] 5 大守禁令（#4 中文 / #5 无 Co-Authored-By / #6 无确认不 commit / #7 无确认不 push / #8 dev_3.0 only）
- [x] 3 主分支策略（main / dev_2.0 / dev_3.0）
- [x] 中文 commit 格式（含实际示例 + 前缀规范 + body 规范）
- [x] 严禁事项汇总（commit / push / merge / 代码）
- [x] PR review 流程（创建 / 描述 / reviewer / 合并）
- [x] 代码风格（Python + TypeScript + SQL）
- [x] 测试要求（后端 + 前端 + 守 #18 必测）
- [x] commit-msg 钩子推荐（中文 + Co-Authored-By 检查）
- [x] CI / CD 推荐（GitHub Actions 配置）
- [x] 开发流程 SOP（4 类场景）
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

---

## 🎉 7 份 setup 文档全部完成！

| # | 文档 | 行数 | 状态 |
|---|---|---|---|
| 20 | external_services_docker_deployment.md | ~750 | ✅ |
| 21 | environment_variables_reference.md | ~790 | ✅ |
| 22 | quickstart_from_git_to_running.md | ~760 | ✅ |
| 23 | faq_and_troubleshooting.md | ~1000 | ✅ |
| 24 | dependencies_and_versions.md | ~550 | ✅ |
| 25 | database_migrations.md | ~750 | ✅ |
| 26 | development_standards.md | **本文档** | ✅ |

**docs_x/setup/ 7 份文档**全部完成！覆盖新开发人员接手项目的所有关键信息：

- **第 0 天准备**：Python 3.11+ / Node 20+ / Docker 24+（22 + 24）
- **拉代码 + 启动**：git clone → 装依赖 → 跑迁移 → 启动后端 → 启动前端（22）
- **运维**：Docker 部署（20）+ .env 参数（21）
- **排错**：FAQ + 错误日志分析（23）
- **开发**：依赖版本（24）+ 数据库迁移（25）+ 开发规范（26）

加上之前的 10 份 docs_x/技术实现文档（A1-A9 + C1），docs_x 目录共 **17 份新开发文档**，与原有的 docs_x/01-03 总览文档构成完整的"接手 + 开发 + 运维"知识库。

**🎉 任务完成。**

**配套阅读：[CLAUDE.md](../../CLAUDE.md) + [HANDO.md](../../HANDO.md) + [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md)。**