# TestAgent Tool系统 技术实现文档

> **配套文档**
> - 总体技术方案: [02_TestAgent_项目总体技术方案.md](../02_TestAgent_项目总体技术方案.md)
> - 源码导读: [03_TestAgent_项目实现原理与源码导读.md](../03_TestAgent_项目实现原理与源码导读.md)
> - 证据索引: [01_TestAgent_项目技术方案证据索引.md](../01_TestAgent_项目技术方案证据索引.md)
> - Context Engine 3.0: [10_TestAgent_ContextEngine3.0_技术实现文档.md](10_TestAgent_ContextEngine3.0_技术实现文档.md)
> - NarrativeComposer: [13_TestAgent_NarrativeComposer_技术实现文档.md](13_TestAgent_NarrativeComposer_技术实现文档.md)
> - 测试方案生成主图: [12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md](12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`
**最后对齐**: 与 F017/F019/F020/F022/F023/F025/F025-ext + 2026-08-19 Bug Fix 同步

> 本文档面向**接手 Tool 系统模块**的开发者，覆盖 **9 个核心 Tool + BaseTool/Registry/Executor/Adapter + 白名单与最小输入校验 + Tool Identity Contract + 与 Context Engine Bridge 集成**。
> 行号以当前 `dev_3.0` HEAD 为准。

---

## 1. 文档说明

### 1.1 模块定位

Tool 系统是 TestAgent 第二阶段（LangGraph v3）的**9 个核心 Tool 集合 + 基础设施**：

- **职责 1**：9 个 Tool 的实现（Requirement / Template / Knowledge / Section / Generate / Regen / Review / Export / FormatCheck）
- **职责 2**：BaseTool 抽象 + ToolRegistry 注册 + ToolExecutor 调用
- **职责 3**：TestAgentToolAdapter 作为 LangGraph 节点唯一入口（白名单 + 输入校验 + 副作用 + 速率）
- **职责 4**：Tool Identity Contract（adapters/test_agent_tool_adapter.py 回写真 event_id/PublicExecutionUpdate）

### 1.2 适用读者

| 角色 | 期望收获 |
|---|---|
| **Tool 维护者** | BaseTool 抽象 + 9 个 Tool 的 inputs/outputs |
| **LangGraph 节点开发者** | TestAgentToolAdapter 的 execute() 入口 |
| **白名单维护者** | DEFAULT_TOOL_WHITELIST 9-tool set |
| **MIG flag 迁移** | _mig_routing.py 的 bridge 路由规则 |
| **Bug 修复者** | 2026-08-19 WordExport 超时 + F025 6 个错误码 + F023 self-healing |

### 1.3 当前状态

- **6,547 行 Tool 代码**（14 文件）+ **936 行 Adapter 代码** = **7,483 行总代码**
- **9 个 Tool 全部实现 BaseTool.run(inputs, context, retry_context)**
- **TestAgentToolAdapter**（38KB，936 行）作为 LangGraph 唯一入口
- **F017/F019/F020/F022/F023/F025/F025-ext 完整闭环**
- **MIG flags** 已接入 8 个 Tool（通过 `_mig_routing.py`）

---

## 2. 总体架构

```mermaid
flowchart TB
    subgraph L1["LangGraph 节点（Pre-confirm / Post-confirm / Format）"]
        N1[parse_requirement_node]
        N2[parse_template_node]
        N3[search_knowledge_node]
        N4[suggest_sections_node]
        N5[generate_test_plan_node]
        N6[review_step / repair_subgraph / regenerate_sections]
        N7[export_word_node]
        N8[check_docx_format_node]
    end
    subgraph L2["TestAgentToolAdapter（adapters/ 936 行）"]
        AD[TestAgentToolAdapter]
        WL[DEFAULT_TOOL_WHITELIST<br/>9-tool set]
        IK[最小输入校验<br/>_REQUIRED_KEYS]
        EM[execute()<br/>白名单→计时→emit→执行→emit]
        LT[last_terminal_event_id<br/>Phase 2.9B.6 锚点]
    end
    subgraph L3["ToolExecutor（executor.py 78 行）"]
        EX[ToolExecutor]
        DC[3-arg → 2-arg fallback<br/>retry_context 兼容]
    end
    subgraph L4["ToolRegistry + register_all_tools"]
        RG[ToolRegistry 单例]
        RA[register_all_tools<br/>9 Tool 实例化]
    end
    subgraph L5["9 个 Tool 实现"]
        T1[RequirementParserTool 376 行]
        T2[TemplateParserTool 219 行]
        T3[KnowledgeSearchTool 378 行]
        T4[SectionSuggestionTool 309 行]
        T5[TestPlanGeneratorTool 904 行]
        T6[TestPlanRegenTool 1168 行]
        T7[ResultReviewTool 1862 行]
        T8[WordExportTool 781 行]
        T9[DocxFormatCheckTool 222 行]
    end
    subgraph L6["External"]
        LLM[LLMClient + ContextInvokerBridge]
        KB[MaaS 知识库]
        ST[Storage / File Reference]
    end

    N1 & N2 & N3 & N4 & N5 & N6 & N7 & N8 --> AD
    AD --> WL & IK & EM & LT
    AD --> EX
    EX --> RG
    RA --> RG
    RG --> T1 & T2 & T3 & T4 & T5 & T6 & T7 & T8 & T9
    T1 & T2 & T8 & T9 --> ST
    T3 --> KB
    T5 & T6 --> LLM
    T5 & T6 -.MIG flag.-> LLM
```

---

## 3. 文件结构（实际 `wc -l` 验证）

### 3.1 backend/app/tools/（14 文件 / 6,547 行）

| 文件 | 行数 | 职责 |
|---|---|---|
| `result_review_tool.py` | **1,862** | **ResultReviewTool**（F025 规则引擎 + 6 个错误码分层 + module coverage / placeholder / empty section 检测）|
| `test_plan_regen_tool.py` | **1,168** | **TestPlanRegenTool**（F023 self-healing + section_ids 反查 + `_canonicalize_ai_targets`）|
| `test_plan_generator_tool.py` | **904** | **TestPlanGeneratorTool**（MIG_GENERATE + ContextInvokerBridge + Three-anchor prompt + schema_feedback retry）|
| `word_export_tool.py` | **781** | **WordExportTool**（F025 template_backfill + format_loss_review + 2026-08-19 项目名 LLM 推断 20s）|
| `knowledge_search_tool.py` | **378** | **KnowledgeSearchTool**（F017 + Maas API + Rerank + degraded fallback）|
| `requirement_parser_tool.py` | **376** | **RequirementParserTool**（F020 OCR/Vision + ImageUnderstandingOrchestrator）|
| `section_suggestion_tool.py` | **309** | **SectionSuggestionTool**（F022 user_constraint + 关键词词典）|
| `docx_format_check_tool.py` | **222** | **DocxFormatCheckTool**（F025-ext + 6 个互斥错误码 + 二次结构丢失检测）|
| `template_parser_tool.py` | **219** | **TemplateParserTool**（Word 模板结构解析）|
| `base.py` | 96 | **BaseTool** 抽象类（run / _success / _error / _progress）|
| `_mig_routing.py` | 94 | **MIG flag bridge 路由**（`invoke_via_bridge_or_none` + `_BridgeRawClient`）|
| `executor.py` | 78 | **ToolExecutor**（按名调用 + retry_context 3-arg/2-arg fallback）|
| `registry.py` | 34 | **ToolRegistry** 单例 + `register/get/list_names/all` |
| `register.py` | 25 | **register_all_tools**（9 Tool 实例化）|
| `__init__.py` | 1 | 模块导出 |

### 3.2 backend/app/agent_runtime/adapters/（1 文件 / 936 行）

| 文件 | 行数 | 职责 |
|---|---|---|
| `test_agent_tool_adapter.py` | **936** | **TestAgentToolAdapter**（白名单 + 输入校验 + 计时 + emit + Identity Contract） |
| `__init__.py` | 22 | 模块导出 |

---

## 4. BaseTool 抽象（`base.py` 96 行）

### 4.1 抽象类

```python
class BaseTool(ABC):
    """Abstract base for all Agent tools.

    Every tool has a unique name, a description, and must implement `run`.
    The optional ``retry_context`` parameter lets a tool observe and
    react to retry attempts (e.g. append corrective feedback to the prompt
    on retry #2).  Tools that don't care about retries simply don't
    accept the parameter (the abstract signature uses kwargs).
    """

    name: str
    description: str

    @abstractmethod
    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: Optional[RetryContext] = None,
    ) -> dict:
        """Execute the tool. Returns a standardised result dict."""
        ...
```

### 4.2 标准化输出信封

```python
def _success(self, data, summary="", warnings=None) -> dict:
    return {
        "success": True,
        "tool_name": self.name,
        "task_id": None,  # filled by executor
        "data": data,
        "summary": summary or f"{self.name} 执行完成",
        "warnings": warnings or [],
        "error": None,
    }

def _error(self, code, message, recoverable=True, warnings=None, details=None) -> dict:
    """Build a standard tool-error dict.

    ``recoverable`` defaults to True so the orchestrator's RetryPolicy will
    retry it (subject to the hard-coded unrecoverable list).
    ``details`` is optional metadata for the orchestrator (e.g. which
    JSON field was missing).  Stored under ``error.details``.
    """
    error_dict = {"code": code, "message": message, "recoverable": recoverable}
    if details:
        error_dict["details"] = details
    return {
        "success": False,
        "tool_name": self.name,
        "task_id": None,
        "data": None,
        "summary": f"{self.name} 执行失败",
        "warnings": warnings or [],
        "error": error_dict,
    }

async def _progress(self, context, message, **metadata) -> None:
    """Emit optional user-facing runtime progress without affecting tool results."""
    emit = getattr(context, "emit_tool_progress", None)
    if not callable(emit):
        return
    await emit(message, **metadata)
```

### 4.3 信封字段

```python
{
    "success": bool,                  # 成功 / 失败
    "tool_name": str,                # 工具名
    "task_id": str | None,           # 由 executor 填充
    "data": dict | None,             # 工具输出
    "summary": str,                  # 一句话摘要
    "warnings": list[str],           # 警告列表
    "error": {
        "code": str,                 # 错误码
        "message": str,              # 错误消息
        "recoverable": bool,         # 是否可重试
        "details": dict | None,      # 元数据（未来 schema_feedback 读取）
    } | None
}
```

---

## 5. ToolRegistry + ToolExecutor（`registry.py` + `executor.py`）

### 5.1 ToolRegistry 单例

```python
class ToolRegistry:
    """In-memory registry of available Agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all(self) -> dict[str, BaseTool]:
        return dict(self._tools)


# Module-level singleton
tool_registry = ToolRegistry()
```

### 5.2 `register_all_tools()`（`register.py`）

```python
def register_all_tools() -> None:
    tool_registry.register(RequirementParserTool())
    tool_registry.register(TemplateParserTool())
    tool_registry.register(KnowledgeSearchTool())
    tool_registry.register(SectionSuggestionTool())
    tool_registry.register(TestPlanGeneratorTool())
    tool_registry.register(TestPlanRegenTool())
    tool_registry.register(ResultReviewTool())
    tool_registry.register(WordExportTool())
    tool_registry.register(DocxFormatCheckTool())
```

### 5.3 ToolExecutor 双签名兼容

```python
class ToolExecutor:
    """Invokes registered tools safely."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def run(self, tool_name, inputs, context, retry_context=None) -> dict:
        tool = self.registry.get(tool_name)
        if tool is None:
            return {
                "success": False, "tool_name": tool_name, "task_id": context.task_id,
                "data": None, "summary": f"未注册的工具: {tool_name}",
                "warnings": [],
                "error": {"code": "TOOL_NOT_FOUND", "message": ..., "recoverable": False},
            }

        try:
            # 3-arg call first; if legacy 2-arg tool, fall back
            try:
                result = await tool.run(inputs, context, retry_context)
            except TypeError as exc:
                if "retry_context" not in str(exc):
                    raise
                result = await tool.run(inputs, context)  # Legacy fallback
            result["task_id"] = context.task_id
            return result
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return {
                "success": False, "tool_name": tool_name, "task_id": context.task_id,
                "data": None, "summary": f"{tool_name} 执行异常",
                "warnings": [],
                "error": {"code": "TOOL_EXECUTION_ERROR", "message": str(exc), "recoverable": False},
            }
```

---

## 6. TestAgentToolAdapter（`adapters/test_agent_tool_adapter.py` 936 行）

### 6.1 设计约束（来自模块自描述）

```python
"""TestAgentToolAdapter — LangGraph 侧唯一调 ToolExecutor 的入口。

设计约束:
* 白名单硬校验(Rule 12):未在白名单的工具 → UnknownToolError
* 不带 AsyncSession / asyncio.Task(Rule 10):session 现场通过 ctx_runtime.session_factory() 取
* 工具信封字节级等价 Legacy:返回 dict 与 Legacy ToolExecutor.run 一致
* 副作用(SSE 事件 / tool_calls 行)与 Legacy orchestrator 完全对应
* 不在 Module 级 import 重型 Legacy 模块,避免 LangGraph 节点 import 链变长
"""
```

### 6.2 9-Tool 白名单

```python
DEFAULT_TOOL_WHITELIST: frozenset[str] = frozenset({
    "RequirementParserTool",
    "TemplateParserTool",
    "KnowledgeSearchTool",
    "SectionSuggestionTool",
    "TestPlanGeneratorTool",
    "TestPlanRegenTool",
    "ResultReviewTool",
    "WordExportTool",
    "DocxFormatCheckTool",
})
```

### 6.3 最小输入校验

```python
_REQUIRED_KEYS: Dict[str, List[str]] = {
    "RequirementParserTool": ["requirement_file_id"],
    "TemplateParserTool": ["template_file_id"],
    "TestPlanRegenTool": ["section_ids", "issues"],
}
```

### 6.4 状态传输字段（17 字段）

```python
_STATE_TRANSFER_FIELDS: tuple[str, ...] = (
    # pre-confirm fields
    "requirement_analysis",
    "template_structure",
    "knowledge_search_result",
    "user_prompt",
    "section_suggestions",
    "section_confirm_config",
    "template_file_id",
    # post-confirm / sub-agent fields
    "test_plan_content",
    "review_standard",
    "review_result",
    "artifact",
    "format_check_result",
    "pending_format_losses",
    "format_loss_confirmation",
)
```

### 6.5 `_AgentContextProxy` dataclass

```python
@dataclass
class _AgentContextProxy:
    """给工具用的最小 AgentContext 视图。

    现场构造,绝不逃出 execute()。不带 AsyncSession —— 工具要 DB 时
    自取 session(本阶段测试中可直接走 ctx_runtime.session_factory())。
    """

    task_id: str
    conversation_id: str
    user_id: str
    user_internal_id: int
    task_internal_id: int
    conversation_internal_id: int
    settings_service: Any
    session_factory: Any = None
    session: Any = None
    context_llm_invoker: Any = None
    llm_client: Any = None
    tool_progress_emitter: Optional[Callable[..., Awaitable[None]]] = None
    requirement_file_id: Optional[str] = None
    template_file_id: Optional[str] = None
    user_prompt: str = ""

    # 工具可能读写的中间数据
    requirement_analysis: Optional[Dict[str, Any]] = None
    template_structure: Optional[Dict[str, Any]] = None
    knowledge_search_result: Optional[Dict[str, Any]] = None
    section_suggestions: Optional[Dict[str, Any]] = None
    section_confirm_config: Optional[Dict[str, Any]] = None
    test_plan_content: Optional[Dict[str, Any]] = None
    review_result: Optional[Dict[str, Any]] = None
    review_standard: Optional[Dict[str, Any]] = None
    artifact: Optional[Dict[str, Any]] = None
    format_check_result: Optional[Dict[str, Any]] = None
    pending_format_losses: Optional[List[Dict[str, Any]]] = None
    format_loss_confirmation: Optional[Dict[str, Any]] = None
    format_loss_timeout_seconds: int = 300

    async def emit_tool_progress(self, progress_message, **metadata):
        if self.tool_progress_emitter is None:
            return
        await self.tool_progress_emitter(progress_message, **metadata)
```

### 6.6 `execute()` 主入口（白名单 → 输入校验 → 计时 → 执行 → 副作用）

```python
async def execute(
    self,
    *,
    tool_name: str,
    inputs: Dict[str, Any],
    ctx_runtime: RuntimeContext,
    attempt: int = 1,
    retry_context: Optional[RetryContext] = None,
    graph_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # ── Rule 12: 白名单
    if tool_name not in self._whitelist:
        raise UnknownToolError(tool_name)

    # ── Rule 13: 最小输入校验
    missing = [k for k in _REQUIRED_KEYS.get(tool_name, []) if not inputs.get(k)]
    if missing:
        logger.warning("ToolAdapter: %s 缺少必填字段 | missing=%s | ...", tool_name, missing, ...)
        return self._missing_inputs_envelope(tool_name=tool_name, attempt=attempt, missing=missing, ...)

    # ── 计时 + 执行
    started_at = self._clock()
    tool_call_id = f"{tool_name}-{uuid4().hex}"
    # Phase 2.9B.6: 每次执行重置终态 event_id
    self._last_terminal_event_id = None
    
    await self._emit_tool_started(tool_name=tool_name, inputs=inputs, attempt=attempt, tool_call_id=tool_call_id, ctx_runtime=ctx_runtime)
    
    session: Any = None
    try:
        async with self._session_factory() as session:
            ctx_proxy = self._build_proxy(tool_name=tool_name, attempt=attempt, tool_call_id=tool_call_id, ctx_runtime=ctx_runtime, inputs=inputs, ...)
            ...
            result = await self._executor.run(tool_name=tool_name, inputs=inputs, context=ctx_proxy, retry_context=retry_context)
            ...
            return result
    except Exception as exc:
        ...
```

### 6.7 `last_terminal_event_id()` — Phase 2.9B.6 锚点

```python
def last_terminal_event_id(self) -> Optional[str]:
    """返回最近一次工具执行的最终 terminal 事件 event_id(可能 None)。

    节点在 execute 返回后调用此方法构造 PendingNarrative.source_event_id,
    不再写空串。
    """
    return self._last_terminal_event_id
```

### 6.8 速率配置

```python
min_visible_seconds: float = 0.8      # Tool 卡片最少显示 0.8s
transition_seconds: float = 0.12    # 状态切换过渡 0.12s
```

### 6.9 5 个公开方法

| 方法 | 职责 |
|---|---|
| `execute(*, tool_name, inputs, ctx_runtime, attempt, retry_context, graph_state)` | 主入口（白名单 → 输入校验 → 计时 → emit → 执行 → emit）|
| `last_terminal_event_id()` | Phase 2.9B.6：最近一次工具执行的真实终态 event_id |
| `_task_public_id_from_state_or_runtime()` | 从 state 或 runtime 提取 task_public_id |
| `_tool_narrative_expected()` | 哪些 Tool 应该有 narrative |
| `_display_tool_name(tool_name)` | UI 显示名映射 |
| `_start_progress_message(tool_name)` | Tool 启动时的 progress 文案 |
| `_completion_message(tool_name, data, fallback)` | Tool 完成时的 summary 文案 |
| `_apply_dynamic_agent_public_update_override(...)` | Dynamic Agent 覆盖 public_update |
| `_semantic_failure_summary(tool_name, data)` | 语义失败的 summary |
| `_safe_tool_error_summary(tool_name, error)` | 安全的 error summary |

### 6.10 异常类

```python
class UnknownToolError(KeyError):
    """tool_name 不在白名单内。"""

class ToolAdapterError(RuntimeError):
    """Adapter 内部意外失败。"""

class ToolExecutorLike(Protocol):
    """ToolExecutor 的最小合约;测试可传 stub。"""
    async def run(self, tool_name, inputs, context, retry_context=None) -> Dict[str, Any]: ...
```

---

## 7. 9 个 Tool 详解

### 7.1 RequirementParserTool（376 行 / F020）

**职责**：解析需求文档（.docx / .txt / .md），提取标题结构、正文、表格、业务模块。

```python
class RequirementParserTool(BaseTool):
    name = "RequirementParserTool"
    description = "解析需求文档，提取标题结构、正文内容、表格、业务模块和功能点"

    async def run(self, inputs, context, retry_context=None) -> dict:
        requirement_file_id = inputs.get("requirement_file_id", "")
        parse_options = inputs.get("parse_options", {}) or {}
        # F020: OCR always-on（parse_options.extract_images 保留向后兼容）
        max_text_length = parse_options.get("max_text_length", 200_000)

        if not requirement_file_id:
            return self._error("REQUIREMENT_FILE_MISSING", ...)
        ...
```

**关键不变量**：
- F020: OCR 总是开启（`parse_options.extract_images` 不再开关，仅用于日志）
- `ImageUnderstandingOrchestrator` 同步处理图片块 → vision JSON / OCR 文本 / 标记
- 失败错误码：`REQUIREMENT_PARSE_FAILED` / `REQUIREMENT_FILE_MISSING`
- 输出 `state.requirement_analysis`（text_content + 结构化字段 + 图片块摘要）

### 7.2 TemplateParserTool（219 行）

**职责**：解析 Word 测试方案模板结构。

- 输入：`{"template_file_id": ...}`
- 输出：`state.template_structure`（章节结构 + 表格占位 + 字段定义）
- 失败错误码：`TEMPLATE_PARSE_FAILED` / `TEMPLATE_FILE_MISSING`

### 7.3 KnowledgeSearchTool（378 行 / F017）

**职责**：公司知识库 MaaS API 检索。

```python
class KnowledgeSearchTool(BaseTool):
    name = "KnowledgeSearchTool"
    description = "检索公司知识库（Maas API + rerank）"
```

**关键不变量**：
- API key 永不离开后端进程
- 明文 key 永不写入 `context.knowledge_search_result`
- KB 不可用 → 主流程继续（仅记录事件供 fallback 决策）
- F019 配置类错误（KNOWLEDGE_NOT_CONFIGURED / KNOWLEDGE_SESSION_MISSING / KNOWLEDGE_API_KEY_INVALID）→ 改为 `_success(degraded)` 而非 `_error`

### 7.4 SectionSuggestionTool（309 行 / F022）

**职责**：基于需求 + 模板 + 用户约束生成章节处理建议。

- 输入：依赖 ctx（自动取 requirement_analysis / template_structure / user_constraint）
- 输出：`state.section_suggestions`（每章节的 `suggested_action`）
- F022 集成：用户约束抽取（UserConstraintExtractor）覆盖默认建议
- 输出经 `narrative_composer.SectionSuggestionContextBuilder` 包装

### 7.5 TestPlanGeneratorTool（904 行）

**职责**：单次 LLM 调用生成结构化测试方案 JSON。

```python
class TestPlanGeneratorTool(BaseTool):
    name = "TestPlanGeneratorTool"
    description = "调用 LLM 生成测试方案的章节 JSON 内容（含自检 + 错误码）"
```

**关键架构**：
- **Three-anchor prompt**（system_rules + call_contract + user_request）
- **MIG_GENERATE** flag：true 走 ContextInvokerBridge（不静默回退）
- **F023 self-healing**：retry_context.strategy == "schema_feedback" 时内部重试 1 次
- **错误码**：JSON_VALIDATION_FAILED / JSON_TRUNCATED / EMPTY_SECTION_CONTENT / SCHEMA_HEADER_MISMATCH / MISSING_SECTION

### 7.6 TestPlanRegenTool（1,168 行）

**职责**：局部重生成（章节级）。

**关键架构**：
- 输入：`{"section_ids": [...], "issues": [...]}`
- **`_canonicalize_ai_targets`**（HANDO §3.5 修复）：把 UI 临时 ID（`section_1`）映射到 `template_structure.generation_config.ai_fields`
- **`_config_alias_groups_from_request`**：从 issue evidence/aliases 反查模板规范字段
- F023 self-healing：retry_context 内部重试

### 7.7 ResultReviewTool（1,862 行 / F025）

**职责**：内容审查（block / warning / passed 三档）。

```python
DEFAULT_SECTIONS: list[str] = [
    "项目概述", "测试目标", "测试范围", "测试策略",
    "测试环境", "测试资源", "测试进度", "准入准出标准",
    "风险分析", "测试交付物",
]
_MIN_SECTION_LENGTH = 50

_PLACEHOLDER_PATTERNS: list[re.Pattern] = [
    re.compile(r"【待补充】"),
    re.compile(r"【待填写】"),
    re.compile(r"请.{0,10}(补充|填写|完善)"),
    re.compile(r"暂无"),
    re.compile(r"待定"),
    ...
]
```

**F025 规则引擎**：
- `ctx.review_standard` 非空 → 走规则引擎，输出 `review_issues[].severity` (block / warn)
- 折叠成 level：block issues → `failed` / warn issues → `warning` / 无 → `passed`
- Backward-compat：`review_standard` 为 None 时只跑硬编码检查

**Phase 2.9A.10 Fail-Fast**：工具执行失败 vs level=warning/failed 严格区分——只后者触发 regen/repair，工具执行失败直接 fail_task。

**6 个错误码分层**（Phase 2.9A.17）来自 `DocxFormatCheckTool`（见 §7.9）

### 7.8 WordExportTool（781 行）

**职责**：高保真模板回填导出 .docx。

```python
class WordExportTool(BaseTool):
    name = "WordExportTool"
    description = "通过 WordExporter.export_from_template 高保真回填导出 docx"
```

**关键架构**：
- 输入：`{"template_file_id": ..., "fidelity": "high|medium|low", "format_loss_review": bool}`
- 输出：`ctx.artifact = {public_id, file_name, file_size, storage_path, ...}`
- fidelity 由 format_loop_count 决定（失败重试时降低保真度）
- Phase 2.9A.X：二次结构丢失（书签/批注/脚注/尾注）→ 不再 task_failed，写 `pending_format_losses` + 挂 `format_loss_interrupt`
- **BUG FIX 2026-08-19**：项目名 LLM 推断 2s → 20s（与 agent_runtime 其他 LLM 调用对齐）
- 失败错误码：`EXPORT_CONTENT_MISSING` / `EXPORT_TEMPLATE_NOT_FOUND` / `EXPORT_TEMPLATE_INVALID` / `EXPORT_ARTIFACT_MISSING_PUBLIC_ID` / `EXPORT_WRITE_FAILED` / `EXPORT_TEMPLATE_BACKFILL_INCOMPATIBLE`

### 7.9 DocxFormatCheckTool（222 行 / F025-ext）

**职责**：检查导出 docx 与模板的结构差异 + 用户可见丢失。

**6 个错误码**（Phase 2.9A.17 互斥语义）：

```python
FORMAT_NO_ARTIFACT = "FORMAT_NO_ARTIFACT"                     # 无 Artifact
FORMAT_ARTIFACT_INVALID = "FORMAT_ARTIFACT_INVALID"           # Artifact 结构不合法
FORMAT_ARTIFACT_PATH_MISSING = "FORMAT_ARTIFACT_PATH_MISSING" # storage_path 为空
FORMAT_ARTIFACT_FILE_NOT_FOUND = "FORMAT_ARTIFACT_FILE_NOT_FOUND" # 物理文件不存在
FORMAT_ARTIFACT_TYPE_UNSUPPORTED = "FORMAT_ARTIFACT_TYPE_UNSUPPORTED"  # 非 docx
FORMAT_CHECK_EXECUTION_FAILED = "FORMAT_CHECK_EXECUTION_FAILED" # 比较器自身异常

# 兼容旧路径
FORMAT_NO_TEMPLATE = "FORMAT_NO_TEMPLATE"
FORMAT_FILE_MISSING = "FORMAT_FILE_MISSING"
FORMAT_CHECK_FAILED = "FORMAT_CHECK_FAILED"
```

**Reads 顺序**（fallback）：`inputs.artifact_path` → `context.artifact["storage_path"]` → `inputs.artifact["storage_path"]`

---

## 8. _mig_routing.py（MIG flag 路由共享 helper / 94 行）

### 8.1 设计目标

```python
"""CE-04 §四：工具层 MIG 路由共享 helper。

给 TestPlanGeneratorTool / TestPlanRegenTool 提供统一的 bridge 调用入口。

- MIG flag=false → 调用方走 legacy（LLMClient.generate）
- MIG flag=true  → 只走 ContextInvokerBridge；bridge 缺失视为明确错误
                   （返回 error 而非静默回退 legacy）。

工具经 context.context_llm_invoker（_AgentContextProxy 透传）访问 bridge。
"""
```

### 8.2 `invoke_via_bridge_or_none()` 主函数

```python
async def invoke_via_bridge_or_none(
    *,
    context: Any,
    call_site: str,
    llm_task_profile: Any,
    current_goal: str,
    output_contract: str = "json",
    task_state_ref: dict | None = None,
) -> tuple[str, Any]:
    """经 bridge 生成。返回 (error_code_or_None, raw_text)。

    bridge 可用 → (None, raw_text)；bridge 缺失/失败 → (error_code, None)。
    绝不静默回退 legacy —— 由调用方根据返回值处理。
    """
    bridge = getattr(context, "context_llm_invoker", None)
    if bridge is None or not getattr(bridge, "available", False):
        return ("MIGRATION_INVOKER_UNAVAILABLE", None)

    try:
        bres = await bridge.generate(
            user_id=getattr(context, "user_internal_id", 0),
            call_site=call_site,
            llm_task_profile=llm_task_profile,
            current_goal=current_goal,
            task_state_ref=task_state_ref,
            output_contract=output_contract,
            user_content=current_goal,
            conversation_id=getattr(context, "conversation_internal_id", None),
            task_id=(
                getattr(context, "task_id", None)
                or getattr(context, "task_internal_id", None)
            ),
            runtime_context=context,
        )
    except Exception as exc:
        return (f"MIGRATION_INVOKER_EXCEPTION:{type(exc).__name__}", None)
    if bres is None or bres.value is None:
        return ("MIGRATION_INVOKER_FAILED", None)
    return (None, bres.value)
```

### 8.3 `_BridgeRawClient` —— bridge 输出伪装为 LLMClient

```python
class _BridgeRawClient:
    """把 bridge 已取回的 raw text 包装为 LLMClient 形状（generate/continue_generation）。

    供工具在 MIG flag=true 时使用：LLM 调用已由 bridge 完成，本类只提供
    ``generate`` / ``continue_generation`` 接口让工具既有解析逻辑不改变。
    第二个参数（provider 重试请求）由工具解析逻辑自行处理。

    Bridge 返回的 ``value`` 是已经过 ``parse_result`` 处理的 Python 对象
    （通常是 dict）。工具下游 ``result_parser.is_json_truncated`` /
    ``parse_and_validate_json`` 仍然按字符串协议工作，所以这里必须把
    非字符串值重新序列化回 JSON 字符串，避免下游 ``.strip()`` AttributeError。
    """

    def __init__(self, raw_text: Any) -> None:
        if isinstance(raw_text, str):
            self._raw = raw_text
        else:
            self._raw = json.dumps(raw_text, ensure_ascii=False, indent=2)

    async def generate(self, *args, **kwargs) -> str:
        return self._raw

    async def continue_generation(self, *args, **kwargs) -> str:
        return ""
```

---

## 9. 工具接入链路（与 v3 主图）

### 9.1 Pre-confirm 链路

```
parse_requirement_node
  → TestAgentToolAdapter.execute(RequirementParserTool, inputs={"requirement_file_id": ...})
  → RequirementParserTool.run(inputs, ctx)
  → 写 state.requirement_analysis + requirement_summary
  → emit tool_started / tool_finished
  → narrative_composer 包装 "需求文档解析" 叙事

parse_template_node
  → TestAgentToolAdapter.execute(TemplateParserTool, inputs={"template_file_id": ...})
  → TemplateParserTool.run(inputs, ctx)
  → 写 state.template_structure

search_knowledge_node
  → TestAgentToolAdapter.execute(KnowledgeSearchTool, inputs={"query": ...})
  → KnowledgeSearchTool.run(inputs, ctx)
  → 调 Maas API + Rerank → 写 state.knowledge_search_result

suggest_sections_node
  → TestAgentToolAdapter.execute(SectionSuggestionTool, inputs={...})
  → SectionSuggestionTool.run(inputs, ctx)
  → F022 user_constraint 覆盖默认建议
  → 写 state.section_suggestions
```

### 9.2 Post-confirm 链路

```
generate_test_plan_node
  → TestAgentToolAdapter.execute(TestPlanGeneratorTool, inputs={...})
  → TestPlanGeneratorTool.run(inputs, ctx, retry_context)
  → MIG_GENERATE=true → ContextInvokerBridge.generate() → 不静默回退
  → 写 state.test_plan_content = {section_package, generated_sections, ...}

review_step / repair_subgraph / regenerate_sections
  → TestAgentToolAdapter.execute(ResultReviewTool / TestPlanRegenTool, inputs={...})
  → review_issues[].severity (block/warn)
  → 折叠成 level (passed/warning/failed) + passed boolean
  → 写 state.review_result
```

### 9.3 Export 链路

```
prepare_export_node
  → 准备 artifact 元数据
export_word_node
  → TestAgentToolAdapter.execute(WordExportTool, inputs={"template_file_id": ..., "fidelity": ...})
  → WordExportTool.run(inputs, ctx)
  → WordExporter.export_from_template()
  → 写 ctx.artifact
  → fidelity 由 format_loop_count 决定（失败重试时降低保真度）
check_docx_format_node
  → TestAgentToolAdapter.execute(DocxFormatCheckTool, inputs={"artifact_path": ...})
  → DocxFormatCheckTool.run(inputs, ctx)
  → 6 个错误码分层 → 写 state.format_check_result + checked_artifact_public_id
```

---

## 10. Tool Identity Contract（来自 HANDO §3.3）

```python
"""Tool Identity Contract (避免「执行了但未显示」)。

- ToolAdapter.wrap() 回写真实 tool_call_id(envelope['tool_call_id'])
- last_terminal_event_id() 真实终态 event_id
- 节点 _build_pending_narrative(source_event_id=adapter.last_terminal_event_id())
- 前端 AgentRunCard.displayToolUpdate 互斥选择器按 (sourceToolCallId, attempt) 锚定
- **禁止**再用 f"{tool_name}-{task_id}" 伪锚点
"""
```

**关键**：
- `tool_call_id = f"{tool_name}-{uuid4().hex}"`（UUID 而非伪锚点）
- `last_terminal_event_id()` 锚定 PendingNarrative.source_event_id
- 前端 reducer 按 `(sourceToolCallId, attempt)` 互斥选择

---

## 11. 测试与验证

### 11.1 测试目录

```
backend/tests/
├── tools/
│   ├── test_base_tool.py                    # BaseTool 抽象
│   ├── test_registry.py                      # ToolRegistry 单例
│   ├── test_executor.py                      # ToolExecutor 兼容
│   ├── test_requirement_parser_tool.py
│   ├── test_template_parser_tool.py
│   ├── test_knowledge_search_tool.py
│   ├── test_section_suggestion_tool.py
│   ├── test_test_plan_generator_tool.py      # 含 F023 self-healing + MIG
│   ├── test_test_plan_regen_tool.py          # 含 section_1 映射修复
│   ├── test_result_review_tool.py            # 含 F025 规则引擎
│   ├── test_word_export_tool.py              # 含 2026-08-19 修复
│   └── test_docx_format_check_tool.py        # 含 F025-ext
├── test_agent_runtime/
│   └── test_test_agent_tool_adapter.py      # 白名单 + 输入校验 + Identity Contract
├── test_template_backfill_compatibility.py
└── test_result_parser_lenient.py
```

### 11.2 关键验证命令

```bash
# 单 Tool 测试
cd backend && python -m pytest tests/tools/test_requirement_parser_tool.py -x -q
cd backend && python -m pytest tests/tools/test_test_plan_generator_tool.py -x -q
cd backend && python -m pytest tests/tools/test_result_review_tool.py -x -q
cd backend && python -m pytest tests/tools/test_word_export_tool.py -x -q

# Tool Adapter 测试
cd backend && python -m pytest tests/agent_runtime/test_test_agent_tool_adapter.py -x -q

# 模板回填兼容性
cd backend && python -m pytest tests/test_template_backfill_compatibility.py -x -q

# F023 self-healing
cd backend && python -m pytest tests/test_repair_parser_fix.py -x -q

# F025 规则
cd backend && python -m pytest tests/test_review_standard_rules.py -x -q
```

### 11.3 端到端验证

```bash
# 启动后端
python -m uvicorn app.main:app --reload

# 1. 上传 docx → 触发测试方案生成
# 2. 验证事件流：tool_started → tool_finished → task_completed
# 3. 验证 SSE payload 包含 tool_call_id（UUID 格式）
# 4. 验证 Artifact 下载 URL 可用
```

---

## 12. 关键架构不变量

> 改动前必须确认的不变量。

| # | 规则 | 证据 | 验证 |
|---|---|---|---|
| 1 | 9 个 Tool 全部继承 `BaseTool`，实现 `name / description / async run()` | `base.py` L12-37 | `grep "class.*BaseTool"` |
| 2 | Tool 标准化输出字段：`success / tool_name / task_id / data / summary / warnings / error` | `base.py` L38-89 | – |
| 3 | `BaseTool._error()` 默认 `recoverable=True`（让 RetryPolicy 重试） | `base.py` L66-69 | – |
| 4 | 9-Tool 白名单硬校验（Rule 12）：未在白名单 → `UnknownToolError` | `test_agent_tool_adapter.py` L60-76 + L216-217 | `grep "DEFAULT_TOOL_WHITELIST"` |
| 5 | Adapter **不带 AsyncSession**（Rule 10）：session 现场通过 `ctx_runtime.session_factory()` | `test_agent_tool_adapter.py` 模块注释 + `_AgentContextProxy` | `grep "session_factory"` |
| 6 | Adapter 工具信封字节级等价 Legacy | `test_agent_tool_adapter.py` 模块注释 | – |
| 7 | `_REQUIRED_KEYS` 最小输入校验：3 个 Tool 有强制 inputs | `test_agent_tool_adapter.py` L36-40 | – |
| 8 | `_STATE_TRANSFER_FIELDS` 17 字段（pre-confirm + post-confirm） | `test_agent_tool_adapter.py` L58-76 | – |
| 9 | ToolExecutor 双签名兼容（3-arg 优先，2-arg fallback）| `executor.py` L52-62 | `grep "TypeError"` |
| 10 | ToolExecutor 异常 → `TOOL_EXECUTION_ERROR` `recoverable=False` | `executor.py` L66-78 | – |
| 11 | Tool ID 格式：`{tool_name}-{uuid4().hex}`（UUID 而非伪锚点）| `test_agent_tool_adapter.py` L240 | `grep "uuid4().hex"` |
| 12 | `last_terminal_event_id()` Phase 2.9B.6 锚点（每次 execute 重置）| `test_agent_tool_adapter.py` L196-203 + L243 | `grep "_last_terminal_event_id"` |
| 13 | `min_visible_seconds=0.8` / `transition_seconds=0.12` 速率配置 | `test_agent_tool_adapter.py` L179-180 | – |
| 14 | MIG flag=true → 走 ContextInvokerBridge，**不静默回退** legacy | `_mig_routing.py` 模块注释 | `grep "不静默回退"` |
| 15 | `invoke_via_bridge_or_none` 返回 `(error_code_or_None, raw_text)` 二元组 | `_mig_routing.py` L29-65 | – |
| 16 | `_BridgeRawClient` 把非字符串值重新序列化回 JSON 字符串 | `_mig_routing.py` L82-86 | – |
| 17 | F020：OCR **always-on**（parse_options.extract_images 仅用于日志）| `requirement_parser_tool.py` L9-12 | `grep "OCR is now always-on"` |
| 18 | F023 self-healing：retry_context.strategy == "schema_feedback" 才内部重试 | `test_plan_generator_tool.py` L21-37 | `grep "schema_feedback"` |
| 19 | F025 规则引擎：`ctx.review_standard` 非空才走规则 | `result_review_tool.py` L20-32 | – |
| 20 | F025 折叠规则：block → failed / warn → warning / 无 → passed | `result_review_tool.py` L23-28 | – |
| 21 | DocxFormatCheckTool **6 个错误码分层**（互斥语义）| `docx_format_check_tool.py` L62-67 | `grep "FORMAT_NO_ARTIFACT"` |
| 22 | DocxFormatCheckTool Reads fallback：`inputs.artifact_path` → `context.artifact["storage_path"]` → `inputs.artifact["storage_path"]` | `docx_format_check_tool.py` L19-21 | – |
| 23 | WordExportTool **BUG FIX 2026-08-19**：项目名 LLM 推断 20s | `word_export_tool.py` L76-79 | `grep "20s"` |
| 24 | TestPlanRegenTool **`_canonicalize_ai_targets`**：UI 临时 ID 映射到模板规范字段 | `result_review_tool.py` 模块注释 | `grep "_canonicalize_ai_targets"` |
| 25 | KnowledgeSearchTool F019 配置类错误 → `_success(degraded)` 而非 `_error` | `knowledge_search_tool.py` L35-39 | – |

---

## 13. 当前限制

### 13.1 真实限制（dev_3.0）

1. **9-tool set 是硬编码白名单**：新增 Tool 必须改 `DEFAULT_TOOL_WHITELIST` + `register_all_tools`
2. **Tool Adapter 单例**：不持多个 Tool Adapter 实例（虽然 constructor 支持）
3. **3 个 Tool 有最小输入校验**：其他 6 个 Tool 依赖工具内部校验
4. **State 传输字段 17 个**：新增 state 字段需修改 `_STATE_TRANSFER_FIELDS`
5. **Knowledge 检索降级**：KB 不可用只记录事件，依赖 narrative 层 fallback
6. **OCR always-on**：增大处理耗时（图片多时）
7. **Format check 6 个错误码**：复杂情况可能需要组合错误码
8. **fidelity 由 format_loop_count 决定**：手动覆盖较繁琐
9. **retry_context 3-arg 兼容**：但老 Tool 不能感知重试信息
10. **MIG flag 测试覆盖**：8 个 Tool 中仅 TestPlanGenerator / TestPlanRegen 接入

### 13.2 后续规划

- **Tool 注册动态化**：从配置文件加载 Tool set
- **Tool 状态传输扩展**：基于 Pydantic 字段反射自动识别
- **更多 Tool 接入 MIG flags**：KnowledgeSearch / ResultReview / WordExport 等
- **Tool Identity Contract 强化**：基于 `(tool_call_id, attempt)` 全局唯一
- **fidelity 智能选择**：基于模板复杂度自动评估

---

## 14. 与其他文档的关系

| 文档 | 关系 |
|---|---|
| [docs_x/02 §1.5](../02_TestAgent_项目总体技术方案.md) | 已实现能力：9 个 Tool |
| [docs_x/02 §13](../02_TestAgent_项目总体技术方案.md) | 工具系统设计 |
| [docs_x/12 测试方案生成主图](12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md) | v3 主图调用 Tool Adapter |
| [docs_x/10 Context Engine 3.0](10_TestAgent_ContextEngine3.0_技术实现文档.md) | Tool 通过 ContextInvokerBridge 接入 CE |
| [docs_x/13 NarrativeComposer](13_TestAgent_NarrativeComposer_技术实现文档.md) | Tool 结果 → ContextBuilder → PublicExecutionUpdate |

---

## 15. 索引自检（dev_3.0）

- [x] backend/app/tools/ 14 文件 / 6,547 行（实际 `wc -l` 验证）
- [x] adapters/test_agent_tool_adapter.py 936 行
- [x] BaseTool 抽象（name / description / run / _success / _error / _progress）
- [x] ToolRegistry 单例 + ToolExecutor 双签名兼容
- [x] TestAgentToolAdapter：9-Tool 白名单 + 17 状态字段 + 最小输入校验
- [x] 9 个 Tool 详解（Requirement / Template / Knowledge / Section / Generator / Regen / Review / Export / FormatCheck）
- [x] _mig_routing.py 路由规则（不静默回退 + _BridgeRawClient）
- [x] Tool Identity Contract（tool_call_id = UUID + last_terminal_event_id）
- [x] F020/F023/F025/F025-ext 关键变更
- [x] BUG FIX 2026-08-19（WordExport 项目名 20s）
- [x] 6 个错误码分层（DocxFormatCheckTool）
- [x] 25 条架构不变量
- [x] 当前限制 + 后续规划
- [x] 与 docs_x/02/10/12/13 文档关系清晰
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[docs_x/02 §13](../02_TestAgent_项目总体技术方案.md) + [docs_x/12 测试方案生成主图](12_TestAgent_测试方案生成主图与三个动态子图_技术实现文档.md)。**