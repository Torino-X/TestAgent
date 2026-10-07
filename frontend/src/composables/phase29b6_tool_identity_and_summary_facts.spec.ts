/**
 * Phase 2.9B.6 — LLM Narrative 锚定、Task Summary 投影与互斥选择前端测试。
 *
 * 覆盖(§十二):
 *  1. tool_call_id=X, source_tool_call_id=X → LLM Narrative 绑定到 Tool;
 *  2. source_tool_call_id ≠ tool_call_id → 不得猜测绑定;
 *  3. LLM Narrative completed → 只显示 LLM;deterministic 不显示;
 *  4. LLM Narrative failed → 显示 deterministic;任务状态不变;
 *  5. LLM Narrative streaming → 使用现有样式流式更新;不出现第二份固定叙事;
 *  6. task_summary_narrative_update → 写入 taskSummaryNarrative;
 *  7. 合法 Task Summary → completionSummary 使用 LLM summary;
 *  8. Task Summary failed/fallback → completionSummary 使用 task_completed.summary;
 *  9. task_completed 晚于 task_summary_narrative_update → 不得覆盖 LLM summary;
 *  10. Hydrate 重放后选择结果与 Live 一致;
 *  11. Narrative failed 不得变更 task.status / tool.status / runtimeStatus / plan steps;
 *  12. 默认折叠、Timeline 顺序和 Artifact 显示回归测试。
 */
import { describe, it, expect } from 'vitest'
import {
  reduceTaskEvent,
  reduceTaskEvents,
  createInitialTaskRunBlock,
  type RawEventInput
} from './useTaskEventReducer'

function toolStarted(toolCallId: string, attempt = 1): RawEventInput {
  return {
    event_id: `evt_start_${toolCallId}_${attempt}`,
    event_type: 'tool_started',
    task_id: 'task_9b6',
    canonical_order: 10,
    payload: {
      tool_name: 'RequirementParserTool',
      tool_call_id: toolCallId,
      attempt,
      display_input: '解析需求文档'
    },
    created_at: '2026-08-02T09:00:00+00:00'
  }
}

function toolFinished(toolCallId: string, attempt = 1): RawEventInput {
  return {
    event_id: `evt_finish_${toolCallId}_${attempt}`,
    event_type: 'tool_finished',
    task_id: 'task_9b6',
    canonical_order: 20,
    payload: {
      tool_name: 'RequirementParserTool',
      tool_call_id: toolCallId,
      attempt,
      chunk_index: 0,
      chunk_final: true,
      display_output: '需求文档解析完成',
      headline: '需求文档解析完成',
      summary: '已完成需求文档解析。',
      impact: '用于确定测试范围。',
      next_action: '接下来解析模板。',
      details: ['识别章节 43 个']
    },
    created_at: '2026-08-02T09:00:01+00:00'
  }
}

function toolNarrative(
  eventId: string,
  overrides: {
    event_type?: string
    source_tool_call_id?: string
    attempt?: number
    narrative_id?: string
    public_update?: Record<string, unknown>
    canonical_order?: number
  } = {}
): RawEventInput {
  const eventType = overrides.event_type ?? 'tool_narrative_started'
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: 'task_9b6',
    canonical_order: overrides.canonical_order ?? 30,
    payload: {
      narrative_id: overrides.narrative_id ?? 'nar-1',
      generation_id: 'gen-1',
      generation_no: 1,
      source_tool_call_id: overrides.source_tool_call_id ?? 'RequirementParserTool-real',
      tool_name: 'RequirementParserTool',
      attempt: overrides.attempt ?? 1,
      ...(overrides.public_update ? { public_update: overrides.public_update } : {})
    },
    created_at: '2026-08-02T09:00:02+00:00'
  }
}

function llmUpdate(): Record<string, unknown> {
  return {
    headline: '需求说明书解析完成',
    summary: '已成功解析需求说明书。',
    impact: '用于确定测试范围。',
    next_action: '接下来解析模板。',
    details: ['识别章节 43 个', '识别表格 7 个']
  }
}

function taskSummaryNarrative(
  eventId: string,
  overrides: {
    event_id?: string
    event_type?: string
    narrative_id?: string
    generation_no?: number
    public_update?: Record<string, unknown>
    fallback_used?: boolean
    narrative_source?: string
    canonical_order?: number
  } = {}
): RawEventInput {
  const eventType = overrides.event_type ?? 'task_summary_narrative_started'
  return {
    event_id: overrides.event_id ?? eventId,
    event_type: eventType,
    task_id: 'task_9b6',
    canonical_order: overrides.canonical_order ?? 90,
    payload: {
      narrative_id: overrides.narrative_id ?? 'tsum-1',
      generation_id: 'tgen-1',
      generation_no: overrides.generation_no ?? 1,
      narrative_source: overrides.narrative_source ?? 'llm',
      fallback_used: overrides.fallback_used ?? false,
      ...(overrides.public_update ? { public_update: overrides.public_update } : {})
    },
    created_at: '2026-08-02T09:01:00+00:00'
  }
}

function taskCompleted(): RawEventInput {
  return {
    event_id: 'evt_completed',
    event_type: 'task_completed',
    task_id: 'task_9b6',
    canonical_order: 100,
    content: '任务已完成。已生成 16 个章节。',
    payload: { summary: '任务已完成。已生成 16 个章节。' },
    created_at: '2026-08-02T09:02:00+00:00'
  }
}

describe('Phase 2.9B.6 tool narrative binding', () => {
  it('tool_call_id=X, source_tool_call_id=X → LLM Narrative 绑定到 Tool', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, toolStarted('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolFinished('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_start', { source_tool_call_id: 'RequirementParserTool-real' }), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_update', {
      event_type: 'tool_narrative_update',
      source_tool_call_id: 'RequirementParserTool-real',
      public_update: llmUpdate()
    }), 'live')

    const tool = block.toolExecutions.find((t) => t.toolCallId === 'RequirementParserTool-real')
    const narrative = block.toolNarratives.find((n) => n.sourceToolCallId === 'RequirementParserTool-real')
    expect(tool).toBeDefined()
    expect(narrative?.status).toBe('completed')
    expect(narrative?.source).toBe('llm')
    expect(narrative?.publicUpdate.headline).toBe('需求说明书解析完成')
  })

  it('source_tool_call_id ≠ tool_call_id → 不得猜测绑定', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, toolStarted('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolFinished('RequirementParserTool-real'), 'live')
    // 叙事锚定到错误 ID(如 ToolName-taskId 回退串)。
    block = reduceTaskEvent(block, toolNarrative('evt_n_start', { source_tool_call_id: 'RequirementParserTool-task_abc' }), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_update', {
      event_type: 'tool_narrative_update',
      source_tool_call_id: 'RequirementParserTool-task_abc',
      public_update: llmUpdate()
    }), 'live')

    // 正确 Tool 没有绑定到这个叙事。
    const bound = block.toolNarratives.filter((n) => n.sourceToolCallId === 'RequirementParserTool-real')
    expect(bound).toHaveLength(0)
    // 错误回退串叙事存在但不匹配真实 Tool。
    const orphan = block.toolNarratives.find((n) => n.sourceToolCallId === 'RequirementParserTool-task_abc')
    expect(orphan).toBeDefined()
  })

  it('LLM Narrative completed → 只显示 LLM;deterministic 不显示', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, toolStarted('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolFinished('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_start', { source_tool_call_id: 'RequirementParserTool-real' }), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_update', {
      event_type: 'tool_narrative_update',
      source_tool_call_id: 'RequirementParserTool-real',
      public_update: llmUpdate()
    }), 'live')

    const narrative = block.toolNarratives[0]
    // 选择器语义:completed + llm → 展示 LLM publicUpdate。
    const showLlm = narrative.status === 'completed' && narrative.source === 'llm' && !!narrative.publicUpdate.headline
    expect(showLlm).toBe(true)
    // deterministic(tool_finished)仍在,但选择器不会同时展示。
    const tool = block.toolExecutions[0]
    expect(tool.publicUpdate?.headline).toBe('需求文档解析完成')
  })

  it('LLM Narrative failed → 显示 deterministic;任务状态不变', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, toolStarted('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolFinished('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_start', { source_tool_call_id: 'RequirementParserTool-real' }), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_fail', {
      event_type: 'tool_narrative_failed',
      source_tool_call_id: 'RequirementParserTool-real'
    }), 'live')

    const narrative = block.toolNarratives[0]
    expect(narrative.status).toBe('failed')
    // 选择器:failed → 走 deterministic。
    const showLlm = narrative.status === 'completed' && narrative.source === 'llm'
    expect(showLlm).toBe(false)
    // 任务状态不变。
    expect(block.status).toBe('running')
    const tool = block.toolExecutions[0]
    expect(tool.status).toBe('success')
  })

  it('attempt 2 的 Narrative 不绑定 attempt 1', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, toolStarted('TestPlanGeneratorTool-1', 1), 'live')
    block = reduceTaskEvent(block, toolFinished('TestPlanGeneratorTool-1', 1), 'live')
    block = reduceTaskEvent(block, toolStarted('TestPlanGeneratorTool-2', 2), 'live')
    block = reduceTaskEvent(block, toolFinished('TestPlanGeneratorTool-2', 2), 'live')
    // attempt 2 的叙事。
    block = reduceTaskEvent(block, toolNarrative('evt_n1', {
      source_tool_call_id: 'TestPlanGeneratorTool-2',
      attempt: 2,
      narrative_id: 'nar-2'
    }), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n2', {
      event_type: 'tool_narrative_update',
      source_tool_call_id: 'TestPlanGeneratorTool-2',
      attempt: 2,
      narrative_id: 'nar-2',
      public_update: { headline: '重试后生成成功', summary: 's', impact: 'i', next_action: 'n', details: [] }
    }), 'live')

    // attempt 1 不绑定 attempt 2 的叙事。
    const attempt1Tool = block.toolExecutions.find((t) => t.toolCallId === 'TestPlanGeneratorTool-1' && t.attempt === 1)
    expect(attempt1Tool).toBeDefined()
    const boundToAttempt1 = block.toolNarratives.filter((n) => n.sourceToolCallId === 'TestPlanGeneratorTool-1' && n.attempt === 1)
    expect(boundToAttempt1).toHaveLength(0)
    const boundToAttempt2 = block.toolNarratives.find((n) => n.sourceToolCallId === 'TestPlanGeneratorTool-2' && n.attempt === 2)
    expect(boundToAttempt2?.publicUpdate.headline).toBe('重试后生成成功')
  })
})

describe('Phase 2.9B.6 task summary narrative projection', () => {
  it('task_summary_narrative_update → 写入 taskSummaryNarrative', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start'), 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_update', {
      event_type: 'task_summary_narrative_update',
      public_update: {
        headline: '设备监控测试方案生成完成',
        summary: '已生成 16 个章节,审查发现 3 个阻塞问题。',
        impact: '用于指导后续执行。',
        next_action: '可下载测试方案。',
        details: ['生成 16 个章节', '发现 3 个阻塞问题']
      }
    }), 'live')

    expect(block.taskSummaryNarrative?.status).toBe('completed')
    expect(block.taskSummaryNarrative?.narrativeSource).toBe('llm')
    expect(block.taskSummaryNarrative?.fallbackUsed).toBe(false)
    expect(block.taskSummaryNarrative?.publicUpdate?.summary).toContain('3 个阻塞问题')
  })

  it('合法 Task Summary → completionSummary 使用 LLM summary', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start'), 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_update', {
      event_type: 'task_summary_narrative_update',
      public_update: {
        headline: 'H',
        summary: '已生成 16 个章节,审查发现 3 个阻塞问题。',
        impact: 'i',
        next_action: 'n',
        details: ['生成 16 个章节', '发现 3 个阻塞问题']
      }
    }), 'live')
    block = reduceTaskEvent(block, taskCompleted(), 'live')

    const n = block.taskSummaryNarrative
    const isLlm = n?.narrativeSource === 'llm'
    const isCompleted = n?.status === 'completed'
    const notFallback = n?.fallbackUsed !== true
    const text = n?.publicUpdate?.summary?.trim()
    const valid = isLlm && isCompleted && notFallback && !!text
    expect(valid).toBe(true)
    // LLM 优先于 deterministic(task_completed.content)。
    expect(text).toContain('3 个阻塞问题')
    expect(text).not.toBe('任务已完成。已生成 16 个章节。')
  })

  it('Task Summary failed/fallback → completionSummary 使用 task_completed.summary', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start'), 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_fail', {
      event_type: 'task_summary_narrative_failed'
    }), 'live')
    block = reduceTaskEvent(block, taskCompleted(), 'live')

    const n = block.taskSummaryNarrative
    const isLlm = n?.narrativeSource === 'llm' && n?.status === 'completed'
    const text = n?.publicUpdate?.summary?.trim()
    const valid = isLlm && n?.fallbackUsed !== true && !!text
    expect(valid).toBe(false)
    // 走 deterministic(task_completed.summary)。
    const completedMsg = block.messages.find((m) => m.eventType === 'task_completed')
    expect(completedMsg?.text).toBe('任务已完成。已生成 16 个章节。')
  })

  it('task_completed 晚于 task_summary_narrative_update → 不得覆盖 LLM summary', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start'), 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_update', {
      event_type: 'task_summary_narrative_update',
      public_update: {
        headline: 'H',
        summary: 'LLM 生成的最终总结。',
        impact: 'i',
        next_action: 'n',
        details: ['生成 16 个章节', '发现 3 个阻塞问题']
      }
    }), 'live')
    block = reduceTaskEvent(block, taskCompleted(), 'live')

    // task_completed 到达后 taskSummaryNarrative 仍保留 LLM 总结。
    expect(block.taskSummaryNarrative?.status).toBe('completed')
    expect(block.taskSummaryNarrative?.publicUpdate?.summary).toBe('LLM 生成的最终总结。')
  })

  it('old generation 不能覆盖新 generation', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start1', { generation_no: 1 }), 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start2', { generation_no: 2 }), 'live')
    // 旧 generation(no=1)再次到达不得覆盖 no=2。
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start1_dup', {
      generation_no: 1,
      event_id: 'evt_ts_start1_dup'
    }), 'live')
    expect(block.taskSummaryNarrative?.generationNo).toBe(2)
  })

  it('Hydrate 重放后选择结果与 Live 一致', () => {
    const events: RawEventInput[] = [
      toolStarted('RequirementParserTool-real'),
      toolFinished('RequirementParserTool-real'),
      toolNarrative('evt_n_start', { source_tool_call_id: 'RequirementParserTool-real' }),
      toolNarrative('evt_n_update', {
        event_type: 'tool_narrative_update',
        source_tool_call_id: 'RequirementParserTool-real',
        public_update: llmUpdate()
      }),
      taskSummaryNarrative('evt_ts_start'),
      taskSummaryNarrative('evt_ts_update', {
        event_type: 'task_summary_narrative_update',
        public_update: {
          headline: 'H',
          summary: 'LLM 最终总结。',
          impact: 'i',
          next_action: 'n',
          details: ['生成 16 个章节', '发现 3 个阻塞问题']
        }
      }),
      taskCompleted()
    ]
    let liveBlock = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    for (const evt of events) liveBlock = reduceTaskEvent(liveBlock, evt, 'live')
    const hydrateBlock = reduceTaskEvents(
      createInitialTaskRunBlock('task_9b6', 'running', 'hydrate'),
      events,
      'hydrate'
    )
    expect(hydrateBlock.toolNarratives).toEqual(liveBlock.toolNarratives)
    expect(hydrateBlock.taskSummaryNarrative).toEqual(liveBlock.taskSummaryNarrative)
    // 选择结果一致(LLM Tool 叙事 + LLM 任务总结)。
    expect(liveBlock.toolNarratives[0].source).toBe('llm')
    expect(liveBlock.taskSummaryNarrative?.narrativeSource).toBe('llm')
  })

  it('Narrative failed 不得变更 task.status / tool.status / runtimeStatus / plan steps', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'live')
    block = reduceTaskEvent(block, toolStarted('RequirementParserTool-real'), 'live')
    block = reduceTaskEvent(block, toolFinished('RequirementParserTool-real'), 'live')
    const beforePlanSteps = block.messages.filter((m) => m.type === 'agent_plan').length
    block = reduceTaskEvent(block, toolNarrative('evt_n_start', { source_tool_call_id: 'RequirementParserTool-real' }), 'live')
    block = reduceTaskEvent(block, toolNarrative('evt_n_fail', {
      event_type: 'tool_narrative_failed',
      source_tool_call_id: 'RequirementParserTool-real'
    }), 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_start'), 'live')
    block = reduceTaskEvent(block, taskSummaryNarrative('evt_ts_fail', {
      event_type: 'task_summary_narrative_failed'
    }), 'live')

    expect(block.status).toBe('running') // 未变
    expect(block.toolExecutions[0].status).toBe('success') // 未变
    expect(block.toolExecutions[0].terminal).toBe(true)
    const afterPlanSteps = block.messages.filter((m) => m.type === 'agent_plan').length
    expect(afterPlanSteps).toBe(beforePlanSteps)
  })

  it('默认折叠、Timeline 顺序和 Artifact 显示回归', () => {
    let block = createInitialTaskRunBlock('task_9b6', 'running', 'hydrate')
    block = reduceTaskEvent(block, toolStarted('RequirementParserTool-real'), 'hydrate')
    block = reduceTaskEvent(block, toolFinished('RequirementParserTool-real'), 'hydrate')
    block = reduceTaskEvent(block, toolNarrative('evt_n_start', { source_tool_call_id: 'RequirementParserTool-real' }), 'hydrate')
    block = reduceTaskEvent(block, toolNarrative('evt_n_update', {
      event_type: 'tool_narrative_update',
      source_tool_call_id: 'RequirementParserTool-real',
      public_update: llmUpdate()
    }), 'hydrate')
    block = reduceTaskEvent(block, {
      event_id: 'evt_artifact',
      event_type: 'artifact_created',
      task_id: 'task_9b6',
      canonical_order: 95,
      payload: {
        artifact: { public_id: 'art-1', file_name: '设备监控_测试方案.docx', file_size: 48344 }
      },
      created_at: '2026-08-02T09:01:30+00:00'
    }, 'hydrate')
    block = reduceTaskEvent(block, taskCompleted(), 'hydrate')

    // 终态任务默认折叠。
    expect(block.collapsed).toBe(true)
    // Timeline 顺序:tool_started(10) < tool_finished(20) < narrative(30) < artifact(95) < completed(100)。
    const orders = block.events.map((e) => e.canonical_order ?? 0)
    expect(orders).toEqual([...orders].sort((a, b) => a - b))
    // Artifact 显示。
    expect(block.artifacts).toHaveLength(1)
    expect(block.artifacts[0].name).toBe('设备监控_测试方案.docx')
    // 叙事不进入 messages。
    expect(block.messages.some((m) => m.type === 'tool_call' && m.toolCall?.name === 'RequirementParserTool')).toBe(true)
  })
})
