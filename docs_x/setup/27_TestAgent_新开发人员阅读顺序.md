# TestAgent 新开发人员文档阅读顺序

> **配套文档**
> - [README.md](../../README.md) — 项目入口
> - [HANDO.md](../../HANDO.md) — 最新接手状态
> - [CLAUDE.md](../../CLAUDE.md) — 项目内 AI 协作指引（人也适用）
> - [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — 完整启动教程
> - [docs_x/01 项目技术方案证据索引](../01_TestAgent_项目技术方案证据索引.md) — 3 份总览文档索引

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`

> 本文档是**新开发人员第一份必读**。覆盖 7 天阅读计划 + 按角色路径 + 速查卡片，让 20 份文档形成清晰的学习路径。
> 接手前先读本文档，再按节奏读其他文档。

---

## 1. 文档总览（20 份）

### 1.1 docs_x/ 完整文档集

| 范围 | 编号 | 文档 | 行数（约） | 角色 |
|---|---|---|---|---|
| **总览** | 01 | 项目技术方案证据索引 | 350 | 所有人 |
| **总览** | 02 | 项目总体技术方案 | 2000 | 所有人 |
| **总览** | 03 | 项目实现原理与源码导读 | 2000 | 所有人 |
| **技术实现** | 10 | Context Engine 3.0 | 1050 | 后端 / 架构 |
| **技术实现** | 11 | 文件识别系统 | 900 | 后端 |
| **技术实现** | 12 | 测试方案生成主图+三个动态子图 | 1070 | 后端 |
| **技术实现** | 13 | NarrativeComposer | 1090 | 后端 / 前端 |
| **技术实现** | 14 | DynamicAgent | 960 | 后端 |
| **技术实现** | 15 | ConversationTimeline+TaskHydration | 1080 | 前端 |
| **技术实现** | 16 | Tool 系统 | 1010 | 后端 |
| **技术实现** | 17 | SSE+LiveEventBus | 1150 | 后端 / SRE |
| **技术实现** | 18 | Checkpointer+持久化 | 1515 | 后端 / SRE |
| **技术实现** | 19 | EngineRouter+灰度发布 | 1050 | 后端 / SRE |
| **Setup** | 20 | external_services_docker_deployment | 750 | 所有人 |
| **Setup** | 21 | environment_variables_reference | 790 | 后端 / SRE |
| **Setup** | 22 | quickstart_from_git_to_running | 760 | 新人必读 |
| **Setup** | 23 | faq_and_troubleshooting | 1000 | 所有人 |
| **Setup** | 24 | dependencies_and_versions | 550 | 所有人 |
| **Setup** | 25 | database_migrations | 750 | 后端 / SRE |
| **Setup** | 26 | development_standards | 760 | 所有人 |

**总计**：20 份，约 **20,000 行** markdown 内容

### 1.2 三层结构

```
docs_x/
├── 总览层（3 份）         →  理解"项目是什么、为什么这样设计"
│   ├── 01 证据索引（入口）
│   ├── 02 总体技术方案（架构）
│   └── 03 源码导读（怎么读）
│
├── 技术实现层（10 份）   →  深入每个能力模块
│   ├── 10-14 后端核心
│   ├── 15 前端核心
│   ├── 16-17 工具 / 事件流
│   └── 18-19 持久化 / 灰度
│
└── Setup 层（7 份）       →  拉代码、跑起来、解决问题
    ├── 20-22 启动三件套
    ├── 23-25 运维四件套
    └── 26-27 开发规范
```

---

## 2. 通用 7 天阅读计划

### 2.1 时间表

| 天 | 任务 | 必读文档 | 输出 |
|---|---|---|---|
| **Day 1** | 环境 + 启动 | README / HANDO / **22** | 看到第一个测试方案生成 |
| **Day 2** | 总览理解 | **01 / 02** | 理解项目架构 / 数据流 |
| **Day 3** | 源码导读 | **03** | 知道读代码的顺序 |
| **Day 4-5** | 技术实现深读 | 10 / 12 / 17 / 18（4 份）| 理解核心模块 |
| **Day 6** | 工具 / Narrative | 13 / 16 / 19 | 理解 Tool + Narrative + 灰度 |
| **Day 7** | 第一个 PR | 26 / 23（备查）| 提第一个 PR |

### 2.2 详细每日计划

#### Day 1：环境 + 启动（4-6 小时）

**目标**：从 0 到跑起来，看到第一个测试方案生成

**必读**：
1. [README.md](../../README.md) — 5 分钟（项目入口）
2. [HANDO.md](../../HANDO.md) — 10 分钟（最新接手状态）
3. [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — **3 小时严格按步骤走**

**操作**：
```bash
git clone <repo>
cd TestAgent
git checkout dev_3.0
# 按 22 号文档走完 9 阶段
```

**验证**：浏览器中能看到第一个测试方案生成全过程

**产出**：本地 dev 环境跑通 + 端到端测试通过

#### Day 2：总览理解（3-4 小时）

**目标**：理解项目架构 / 数据流 / 关键决策

**必读**：
1. [01 项目技术方案证据索引](../01_TestAgent_项目技术方案证据索引.md) — 30 分钟（建立索引）
2. [02 项目总体技术方案](../02_TestAgent_项目总体技术方案.md) — 3 小时（重点：第 1 / 2 / 6 / 7 / 8 / 30 章）

**重点关注**：
- §1.4 已实现能力清单
- §2 项目背景与产品定位
- §6.2 章节确认流程
- §7 后端分层架构
- §8 Agent 总体架构
- §30 部署架构

**产出**：能向别人讲清"这个项目做什么、怎么做的"

#### Day 3：源码导读（3-4 小时）

**目标**：知道读代码的顺序

**必读**：
1. [03 项目实现原理与源码导读](../03_TestAgent_项目实现原理与源码导读.md) — 3 小时

**重点关注**：
- §1 阅读方法（10 步顺序）
- §2 项目目录总览（含完整目录树）
- §23 新开发人员七天阅读计划（你自己正在用的！）

**操作**：打开 IDE，对照 §1 的 10 步顺序读 backend 代码

**产出**：能独立读 backend/ 任何模块

#### Day 4-5：技术实现深读（每天 6-8 小时，共 2 天）

**目标**：深入 4 个核心模块

**Day 4 上午**（3 小时）：[**17 SSE+LiveEventBus**](../17_TestAgent_SSE与LiveEventBus_技术实现文档.md)
- 理解实时事件流 + LiveEventBus + InFlightRegistry

**Day 4 下午**（3 小时）：[**18 Checkpointer+持久化**](../18_TestAgent_Checkpointer与持久化_技术实现文档.md)
- 理解 Postgres Checkpointer + 多 worker 协调

**Day 5 上午**（3 小时）：[**10 Context Engine 3.0**](../10_TestAgent_ContextEngine3.0_技术实现文档.md)
- 理解 Context Engine + Profile + Source + Composer

**Day 5 下午**（3 小时）：[**12 测试方案生成主图+三个动态子图**](../12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)
- 理解 LangGraph v3 主图 + 3 个子图

**产出**：能独立修改核心模块

#### Day 6：Tool + Narrative + 灰度（4-6 小时）

**目标**：理解 Tool 系统 / Narrative / 灰度

**必读**：
1. [**16 Tool 系统**](../16_TestAgent_Tool系统_技术实现文档.md) — 2 小时
2. [**13 NarrativeComposer**](../13_TestAgent_NarrativeComposer_技术实现文档.md) — 2 小时
3. [**19 EngineRouter+灰度发布**](../19_TestAgent_EngineRouter与灰度发布_技术实现文档.md) — 2 小时

**产出**：能新增 / 修改 Tool / 改 Narrative 模板

#### Day 7：第一个 PR（4-6 小时）

**目标**：按规范提交第一个 PR

**必读**：
1. [26 development_standards](26_TestAgent_开发规范.md) — 30 分钟（commit 规范 + PR 流程）

**操作**：
```bash
# 1) 找一个 TODO 注释或小 bug
git checkout -b fix/typo-in-readme
# 2) 改代码 + 写测试
# 3) 中文 commit（守 #4）
git add .
git commit -m "修复:README 拼写错误"
# ⚠️ 守 #5：不加 Co-Authored-By
# ⚠️ 守 #6：先问用户是否 commit / push
# 4) 提 PR 到 dev_3.0
```

**产出**：第一个 PR 合入

---

## 3. 按角色阅读路径

### 3.1 新后端开发（主路径）

```
Day 1: 22（启动） → 验证能跑
Day 2: 02（架构总览）
Day 3: 03（源码导读）
Day 4: 17（SSE）+ 18（Checkpointer）
Day 5: 10（Context Engine）+ 12（主图+子图）
Day 6: 16（Tool）+ 13（Narrative）+ 19（灰度）
Day 7: 26（开发规范）+ 提 PR
```

**后续**：按工作内容选读
- 改 Tool → 16
- 改 Narrative → 13
- 改主图 → 12
- 改 Context Engine → 10
- 改 SSE / 事件流 → 17
- 改 Checkpointer / 多 worker → 18
- 改灰度 → 19
- 改文件识别 → 11
- 改 DynamicAgent → 14
- 改前端 reducer → 15

### 3.2 新前端开发

```
Day 1: 22（启动）→ 浏览器看到运行
Day 2: 02 § 5-6（前端架构）
Day 3: 03（源码导读）
Day 4: 15（ConversationTimeline + TaskHydration）⭐ 核心
Day 5: 13 § 11-13（Narrative 事件）+ 17 § 5-6（SSE + 事件 reducer）
Day 6: 19（EngineRouter 灰度对前端影响）
Day 7: 26 + 提 PR
```

**核心文档**：**15 ConversationTimeline + TaskHydration** —— 这是前端最关键的文档

**其他参考**：
- [frontend/AGENTS.md](../../frontend/AGENTS.md) — 前端技术栈约束
- [docs_x/02 §5-6 前端架构](../02_TestAgent_项目总体技术方案.md)
- [docs_x/15 § 5-12 TaskRunReducer 详解](../15_TestAgent_ConversationTimeline与TaskHydration_技术实现文档.md)

### 3.3 新 SRE / 运维

```
Day 1: 22（启动）→ 看到所有服务跑起来
Day 2: 20（外部服务 Docker）
Day 3: 17（LiveEventBus）+ 18（Checkpointer 多 worker）
Day 4: 19（灰度发布）+ 25（数据库迁移）
Day 5: 21（.env 参数）+ 23（FAQ on-call 流程）
Day 6: 02 § 30-31（部署架构）
Day 7: 24（依赖版本）
```

**核心文档**：
- 20（外部服务）⭐
- 17 + 18（多 worker 协调）⭐
- 19（灰度）
- 23（FAQ / on-call）

### 3.4 快速参考（高级用户）

如果你已经熟悉 LangGraph / Vue / DevOps，只是想了解本项目：

| 想了解 | 读哪份 | 时间 |
|---|---|---|
| 5 分钟了解项目 | README + HANDO | 15 分钟 |
| 30 分钟看架构 | 01 + 02 § 1-3 | 30 分钟 |
| 知道怎么读代码 | 03 | 1 小时 |
| 看具体模块 | 10-19 任选 | 1-2 小时/份 |
| 跑起来 / 部署 | 22 + 20 + 21 | 1 小时 |
| 解决问题 | 23 | 按需 |

---

## 4. 速查卡片（按场景）

### 4.1 "我想启动项目"

| 步骤 | 文档 |
|---|---|
| 1. 装 Python / Node / Docker | 22 § 2 |
| 2. 拉代码 + 切分支 | 22 § 3 |
| 3. 装 Python 依赖 | 22 § 4 |
| 4. 启动外部服务（dev） | 22 § 5 + 20 § 4 |
| 5. 配置 .env | 22 § 4 + 21 |
| 6. 跑迁移 | 22 § 6 + 25 |
| 7. 启动后端 + 前端 | 22 § 7-8 |
| 8. 端到端验证 | 22 § 9 |

**总用时**：30-60 分钟（按 22 号严格走）

### 4.2 "我遇到了 bug"

| 步骤 | 文档 |
|---|---|
| 1. 看后端日志关键字 | 23 § 9 |
| 2. 查常见错误码 | 23 § 1.2 + 23 § 12 |
| 3. 看具体模块 FAQ | 23 § 2-7 |
| 4. 看守 #X 相关不变量 | 10-19 不变量小节 |
| 5. 看 HANDO 已知问题 | HANDO § 4 |

### 4.3 "我要改 X 模块"

| 模块 | 主文档 | 相关 |
|---|---|---|
| Context Engine | 10 | 13（Narrative 用 CE）|
| 文件识别 | 11 | 16（Tool 链路）|
| 测试方案生成 | 12 | 16 + 19（灰度）|
| Narrative | 13 | 15（前端消费）|
| DynamicAgent | 14 | 10 + 16 |
| 前端 Timeline | 15 | 13（事件来源）|
| Tool 系统 | 16 | 11 + 12 + 14 |
| SSE / LiveEventBus | 17 | 15 + 18 |
| Checkpointer | 18 | 19（灰度）|
| 灰度发布 | 19 | 18 + 25 |
| 外部服务 | 20 | 21 + 25 |
| .env | 21 | 全部 |
| 启动 | 22 | 20 + 25 |
| 故障排查 | 23 | 22 + 17 + 18 |
| 依赖版本 | 24 | 22 + 25 |
| 数据库迁移 | 25 | 18 + 19 |
| 开发规范 | 26 | – |

### 4.4 "我要部署到生产"

| 步骤 | 文档 |
|---|---|
| 1. 准备云 MySQL | 20 § 3.1 |
| 2. 启动 PG + Redis 容器 | 20 § 6 |
| 3. 写 .env.prod（600 权限）| 20 § 6.3 + 21 |
| 4. 跑迁移 | 25 § 6 |
| 5. 启动后端 + 前端 + nginx | 20 § 6.3 + 22 |
| 6. 申请 HTTPS cert | 20 § 6.3 |
| 7. 验证健康 | 22 § 11.1 |
| 8. 8 阈值评估 | 19 § 7 |
| 9. AutoRollback 启用 | 19 § 9 |

---

## 5. 关键决策点（理解项目必知）

读完 Day 1-3 后，你应该能回答这些问题：

### 5.1 架构决策

- **Q：为什么用 LangGraph v3 而不是继续用 LangChain Agent？**
  - 答：见 [12 § 1 + 19 § 1](../12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)：可恢复 + Checkpoint + 多 worker + Interrupt

- **Q：为什么有 Legacy + LangGraph 双引擎？**
  - 答：见 [19 § 1 + 19 § 2](../19_TestAgent_EngineRouter与灰度发布_技术实现文档.md)：历史任务兼容 + 灰度发布 + 守 #18

- **Q：为什么有 Narrative Composer？**
  - 答：见 [13 § 1](../13_TestAgent_NarrativeComposer_技术实现文档.md)：LLM-first 叙事 + 流式输出 + 8 Tool 同步 Barrier

- **Q：为什么用 Postgres 而不只用 MySQL 做 Checkpointer？**
  - 答：见 [18 § 1](../18_TestAgent_Checkpointer与持久化_技术实现文档.md)：langgraph-checkpoint-postgres 2.0.25 实际用 psycopg v3

### 5.2 守禁令决策

- **Q：守 #18（Postgres 不可用不静默 fallback）为什么强制关闭 LangGraph？**
  - 答：避免生产任务在 MemorySaver 上跑（重启丢状态）

- **Q：守 #1（生产必须 Postgres）为什么这么严？**
  - 答：MySQL 没有 checkpoint 表，LangGraph 状态无法持久化

- **Q：守 #2（默认 langgraph）为什么改了？**
  - 答：Phase 2.8R-K dev 友好 — 未设 env flag 时默认走 langgraph

### 5.3 模块边界

- **Q：PreparationAgent / RepairAgent / IncrementalAgent 有什么区别？**
  - 答：见 [12 § 2-4](../12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)：准备 / 复审修复 / 增量修改

- **Q：Context Engine 跟 LangGraph 什么关系？**
  - 答：CE 是 LangGraph 节点调用前的"context 准备层"，通过 ContextInvokerBridge 接入

---

## 6. 常见问题（按角色）

### 6.1 新人第一周常问

| 问题 | 答 |
|---|---|
| "项目用什么前端框架？" | Vue 3 + TS + Vite + Naive UI（见 [frontend/AGENTS.md](../../frontend/AGENTS.md)）|
| "后端用 Python 哪个版本？" | 3.11+（推荐 3.12）（见 [24 § 2.1](24_TestAgent_依赖与版本管理.md)）|
| "LLM 用什么？" | OpenAI 兼容协议，可换 Azure / 自部署（见 [21 § 6](21_TestAgent_env参数详解.md)）|
| "测试怎么跑？" | `pytest tests/ -x -q`（后端）/ `npm run test`（前端）（见 [22 § 10](22_TestAgent_从Git到启动完整教程.md)）|
| "commit 怎么写？" | 中文（守 #4），不加 Co-Authored-By（守 #5）（见 [26 § 2](26_TestAgent_开发规范.md)）|

### 6.2 进阶问题

| 问题 | 答 |
|---|---|
| "怎么加新 Tool？" | 见 [16 § 3-6](../16_TestAgent_Tool系统_技术实现文档.md)：BaseTool + ToolAdapter + 白名单 |
| "怎么加新 graph 节点？" | 见 [12 § 3](../12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)：v3 主图 + nodes_*.py |
| "怎么扩 Context Engine source？" | 见 [10 § 9](../10_TestAgent_ContextEngine3.0_技术实现文档.md)：sources/ + registry |
| "怎么发版？" | 见 [26 § 3.3](26_TestAgent_开发规范.md)：hotfix 分支 → 合 main |

---

## 7. 文档依赖关系图

```
                        README.md
                            │
                       HANDO.md (接手状态)
                            │
                ┌───────────┴───────────┐
                │                       │
        22_quickstart             CLAUDE.md (协作)
                │
                ▼
    ┌───────────┴───────────┐
    │                       │
docs_x/01             docs_x/02
(证据索引)            (总体方案)
    │                       │
    └───────┬───────────────┘
            │
       docs_x/03 (源码导读)
            │
   ┌────────┼────────┬────────┬────────┐
   │        │        │        │        │
  10      12       16       17       18
  CE     主图+子图  Tool     SSE    Checkpointer
   │        │        │        │        │
   │        │        │        │        │
  11      13       15       19       
 文件识别 Narrative  Timeline 灰度   
   │        │        │        
   │        │        │        
  14      20-26            
Dynamic  Setup 7 份
 Agent
```

---

## 8. 关键守禁令速查（每天提醒）

| 守 | 内容 | 出现文档 |
|---|---|---|
| **#1** | LangGraph 必须 Postgres checkpointer | 18 / 19 |
| **#2** | 默认 langgraph（Phase 2.8R-K）| 19 |
| **#4** | commit 中文 | 26 |
| **#5** | 不加 Co-Authored-By | 26 |
| **#6** | 不自动 commit | 26 |
| **#7** | 不自动 push | 26 |
| **#8** | dev_3.0 only | 26 |
| **#18** | Postgres 不可用 → 强制 production_dispatch_forced_off | 18 / 19 |

**违反守门 = 启动失败 / 误发布 / 代码风格破裂**

---

## 9. 完成度自检

按本文档顺序读完，你应该能：

- [ ] 启动 dev 环境（Day 1）
- [ ] 理解项目架构（Day 2-3）
- [ ] 独立读 backend 代码（Day 4-5）
- [ ] 独立读 frontend 代码（Day 4-5）
- [ ] 修改小 bug 并提 PR（Day 7）
- [ ] 回答项目关键决策（架构 / 守门 / 模块边界）

---

## 10. 索引自检

- [x] 20 份文档总览（3 总览 + 10 技术 + 7 setup）
- [x] 通用 7 天阅读计划（Day 1-7 详细）
- [x] 4 类角色路径（后端 / 前端 / SRE / 快速参考）
- [x] 速查卡片（启动 / 排错 / 改模块 / 部署）
- [x] 关键决策点（架构 / 守门 / 模块边界）
- [x] 常见问题（新人第一周 + 进阶）
- [x] 文档依赖关系图
- [x] 关键守禁令速查（8 条）
- [x] 完成度自检 checklist
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

---

## 索引

| 配套 | 链接 |
|---|---|
| 项目入口 | [README.md](../../README.md) |
| 最新状态 | [HANDO.md](../../HANDO.md) |
| AI 协作指引 | [CLAUDE.md](../../CLAUDE.md) |
| 启动教程 | [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) |
| 总览文档 | [docs_x/01-03](../01_TestAgent_项目技术方案证据索引.md) |
| 技术实现 | [docs_x/10-19](../) |
| Setup 文档 | [docs_x/20-26](20_TestAgent_外部服务Docker部署教程.md) |

**完成阅读后，你可以：**
- 接新需求
- 修 bug
- 提 PR
- 部署到生产
- 救火

**一切文档已就位。** 🎉