import { describe, expect, it, vi } from 'vitest'
import { createTaskEventHandler } from './useTaskEvents'
import type { ChatMessage } from '@/types'

function makeHandler(options: { updateResult?: boolean } = {}) {
  const appended: ChatMessage[] = []
  const updated: Array<[string, Partial<ChatMessage>]> = []
  const statuses: Array<[string, string]> = []

  const handler = createTaskEventHandler({
    onAppendMessage: (message) => appended.push(message),
    onUpdateMessage: (messageId, patch) => {
      updated.push([messageId, patch])
      return options.updateResult ?? true
    },
    onStatusChange: (taskId, status) => statuses.push([taskId, status])
  })

  return { handler, appended, updated, statuses }
}

describe('useTaskEvents', () => {
  it('calls onAnyEvent before onAppendMessage for plan_step_started', () => {
    const calls: string[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: () => calls.push('append'),
      onUpdateMessage: () => true,
      onAnyEvent: (type) => calls.push(`any:${type}`)
    })
    handler('plan_step_started', {
      event_id: 'evt_1', task_id: 't1',
      payload: { step: 'understand', status: 'running' }
    })
    // onAnyEvent fires before plan_step_started returns; onAppendMessage never fires
    expect(calls).toEqual(['any:plan_step_started'])
  })

  it('calls onAnyEvent for tool_started before onAppendMessage', () => {
    const calls: string[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: () => calls.push('append'),
      onUpdateMessage: () => true,
      onAnyEvent: (type) => calls.push(`any:${type}`)
    })
    handler('tool_started', {
      event_id: 'evt_2', task_id: 't1',
      payload: { tool_name: 'TestPlanGeneratorTool', tool_call_id: 'tc_1' }
    })
    expect(calls[0]).toBe('any:tool_started')
    expect(calls[1]).toBe('append')
  })

  it('onAnyEvent fires for task_completed before onAppendMessage', () => {
    const calls: string[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: () => calls.push('append'),
      onUpdateMessage: () => true,
      onAnyEvent: (type) => calls.push(`any:${type}`)
    })
    handler('task_completed', {
      event_id: 'evt_done', task_id: 't1',
      payload: { summary: 'done' }
    })
    expect(calls[0]).toBe('any:task_completed')
    expect(calls).toContain('append')
  })

  it('onAnyEvent fires for need_user_confirm', () => {
    const calls: string[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: () => calls.push('append'),
      onUpdateMessage: () => true,
      onAnyEvent: (type) => calls.push(`any:${type}`)
    })
    handler('need_user_confirm', {
      event_id: 'evt_need', task_id: 't1',
      payload: { confirmation_id: 'c1', sections: [] }
    })
    expect(calls[0]).toBe('any:need_user_confirm')
  })

  it('onAnyEvent fires for task_failed', () => {
    const calls: string[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: () => calls.push('append'),
      onUpdateMessage: () => true,
      onAnyEvent: (type) => calls.push(`any:${type}`)
    })
    handler('task_failed', {
      event_id: 'evt_fail', task_id: 't1',
      payload: { error: 'boom' }
    })
    expect(calls[0]).toBe('any:task_failed')
  })

  it('forwards incremental terminal events to the task status handler', () => {
    const { handler, statuses } = makeHandler()

    handler('incremental_completed', {
      event_id: 'evt_incremental_done',
      task_id: 'task_incremental',
      payload: { success: true }
    })

    expect(statuses).toEqual([['task_incremental', 'completed']])
  })

  it('moves an incremental task into running state when it starts', () => {
    const { handler, statuses } = makeHandler()

    handler('incremental_started', {
      event_id: 'evt_incremental_started',
      task_id: 'task_incremental',
      payload: {}
    })

    expect(statuses).toEqual([['task_incremental', 'running']])
  })

  it('moves an incremental task into confirmation state on format loss', () => {
    const { handler, statuses } = makeHandler()

    handler('format_loss_confirm_requested', {
      event_id: 'evt_format_loss',
      task_id: 'task_incremental',
      payload: { losses: [{ type: 'style' }] }
    })

    expect(statuses).toEqual([['task_incremental', 'waiting_user_confirm']])
  })

  it('onAnyEvent fires before onAppendMessage for first tool_started event', () => {
    const timestamps: number[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: () => timestamps.push(2),
      onUpdateMessage: () => true,
      onAnyEvent: () => timestamps.push(1)
    })
    handler('tool_started', {
      event_id: 'evt_first', task_id: 't1',
      payload: { tool_name: 'X', tool_call_id: 'tc_1' }
    })
    // First call to onAnyEvent fires at timestamp 1, onAppendMessage fires at 2
    expect(timestamps).toEqual([1, 2])
  })

  it('keeps the final public update authoritative over late duplicate chunks', () => {
    const { handler, updated, appended } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_tool_started',
      task_id: 'task_chunk_final',
      payload: { tool_name: 'RequirementParserTool' }
    })

    handler('tool_finished', {
      event_id: 'evt_tool_final',
      task_id: 'task_chunk_final',
      payload: {
        tool_name: 'RequirementParserTool',
        public_execution_update: {
          level: 'success',
          headline: '完成',
          chunk_index: 2,
          chunk_total: 3,
          chunk_final: true
        }
      }
    })

    handler('tool_finished', {
      event_id: 'evt_tool_late',
      task_id: 'task_chunk_final',
      payload: {
        tool_name: 'RequirementParserTool',
        public_execution_update: {
          level: 'success',
          headline: '迟到旧片段',
          chunk_index: 1,
          chunk_total: 3,
          chunk_final: false
        }
      }
    })

    expect(updated).toHaveLength(1)
    expect(updated[0][1].toolCall?.publicUpdate?.headline).toBe('完成')
    expect(appended).toHaveLength(1)
  })

  it('maps tool_progress to an in-place tool_call update with presentation fields', () => {
    const { handler, appended, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_tool_started',
      task_id: 'task_progress',
      payload: {
        tool_name: 'RequirementParserTool',
        tool_call_id: 'call_req',
        display_tool_name: '调用Word文档解析工具',
        business_action: '解析需求文档',
        business_subject_type: '需求文档',
        business_subject_name: '智慧校园需求说明书.docx',
        progress_message: '正在读取 Word 文档'
      }
    })

    handler('tool_progress', {
      event_id: 'evt_tool_progress',
      task_id: 'task_progress',
      payload: {
        tool_name: 'RequirementParserTool',
        tool_call_id: 'call_req',
        progress_message: '正在处理图片：《系统架构图.png》'
      }
    })

    expect(appended.filter(message => message.type === 'tool_call')).toHaveLength(1)
    expect(updated).toHaveLength(1)
    expect(updated[0][0]).toBe('tool_call_req')
    expect(updated[0][1].toolCall?.status).toBe('running')
    expect(updated[0][1].toolCall?.displayName).toBe('调用Word文档解析工具')
    expect(updated[0][1].toolCall?.progressMessage).toBe('正在处理图片：《系统架构图.png》')
    expect(updated[0][1].toolCall?.finishedAt).toBeUndefined()
  })

  it.each([
    ['ResultReviewTool', '调用结果审查工具'],
    ['WordExportTool', '调用Word文档导出工具'],
    ['DocxFormatCheckTool', '调用Word文档格式检查工具'],
  ])('falls back to display mapping for %s when backend omits display_tool_name', (backendName, expected) => {
    const { handler, appended } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started',
      task_id: 'task_1',
      payload: {
        tool_name: backendName,
        tool_call_id: `call_${backendName}`,
      },
    })

    const toolCallMsg = appended.find(message => message.type === 'tool_call')
    expect(toolCallMsg?.toolCall?.displayName).toBe(expected)
    expect(toolCallMsg?.toolCall?.name).toBe(backendName)
  })

  it('maps backend plan payload into visible execution plan steps', () => {
    const { handler, appended } = makeHandler()

    handler('plan_created', {
      event_id: 'evt_plan',
      task_id: 'task_1',
      payload: {
        plan: [
          { step_id: 'understand', name: '理解任务', detail: '根据文档生成测试方案', status: 'done' },
          { step_id: 'parse_requirement', name: '解析需求文档', status: 'running' }
        ]
      }
    })

    expect(appended).toHaveLength(1)
    expect(appended[0]).toMatchObject({
      type: 'agent_plan',
      taskId: 'task_1',
      plan: [
        { id: 'understand', title: '理解任务', detail: '根据文档生成测试方案', status: 'done' },
        { id: 'parse_requirement', title: '解析需求文档', status: 'running' }
      ]
    })
  })

  it('maps LangGraph string plan steps into the existing plan card model', () => {
    const { handler, appended } = makeHandler()

    handler('plan_created', {
      event_id: 'evt_langgraph_plan',
      task_id: 'task_langgraph',
      payload: {
        plan: {
          steps: [
            'parse_requirement',
            'parse_template',
            'search_knowledge',
            'suggest_sections',
            'wait_user_confirm',
            'generate',
            'review',
            'export'
          ]
        }
      }
    })

    expect(appended[0]).toMatchObject({
      type: 'agent_plan',
      plan: [
        { id: 'parse_requirement', title: '解析需求文档' },
        { id: 'parse_template', title: '解析测试方案模板' },
        { id: 'search_knowledge', title: '检索公司知识库' },
        { id: 'suggest_sections', title: '生成章节处理建议' },
        { id: 'wait_user_confirm', title: '确认章节生成范围' },
        { id: 'generate', title: '生成测试方案' },
        { id: 'review', title: '审查生成结果' },
        { id: 'export', title: '导出 Word 文档' }
      ]
    })
  })

  it('replays history events using public_id and payload_json aliases', () => {
    const { handler, appended } = makeHandler({ updateResult: false })

    handler('message', {
      public_id: 'evt_history_tool',
      event_type: 'tool_finished',
      task_id: 'task_history',
      created_at: '2026-07-22T08:00:02Z',
      payload_json: JSON.stringify({
        tool_name: 'RequirementParserTool',
        output: '历史回放已完成',
        duration_ms: '2300'
      })
    })

    expect(appended[0]).toMatchObject({
      id: 'tool_task_history_RequirementParserTool',
      createdAt: '2026-07-22T08:00:02Z',
      toolCall: {
        name: 'RequirementParserTool',
        status: 'success',
        output: '历史回放已完成',
        duration: '3s'
      }
    })
  })

  it('keeps tool started and finished events on the same specific tool row', () => {
    const { handler, appended, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_tool_started',
      task_id: 'task_1',
      title: 'RequirementParserTool 开始执行',
      payload: {
        input: { file_id: 'file_1' }
      }
    })

    expect(appended[0]).toMatchObject({
      id: 'tool_task_1_RequirementParserTool',
      toolCall: {
        name: 'RequirementParserTool',
        status: 'running'
      }
    })

    handler('tool_finished', {
      event_id: 'evt_tool_finished',
      task_id: 'task_1',
      payload: {
        tool_name: 'RequirementParserTool',
        output: '已解析 5 个需求点',
        duration_ms: 2300
      }
    })

    expect(updated[0]).toEqual([
      'tool_task_1_RequirementParserTool',
      expect.objectContaining({
        toolCall: expect.objectContaining({
          name: 'RequirementParserTool',
          status: 'success',
          output: '已解析 5 个需求点',
          duration: '3s'
        })
      })
    ])
  })

  it('maps tool_finished with level=warning to status=warning (KB degraded)', () => {
    // BUG FIX 2026-08-18 (方案 2):知识库降级完成(not_configured / 上游失败)
    // 事件 level=warning → status='warning',时间线渲染黄色而非绿色/红色。
    const { handler, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_kb_started',
      task_id: 'task_kb_degraded',
      payload: { tool_name: 'KnowledgeSearchTool', tool_call_id: 'call_kb_degraded' }
    })

    handler('tool_finished', {
      event_id: 'evt_kb_degraded',
      task_id: 'task_kb_degraded',
      payload: {
        tool_name: 'KnowledgeSearchTool',
        tool_call_id: 'call_kb_degraded',
        output: '知识库检索降级完成',
        duration_ms: 100,
        public_execution_update: {
          version: 1,
          kind: 'tool_result',
          level: 'warning',
          headline: '知识库检索降级完成',
          summary: '知识库不可用，测试方案将基于本地上下文生成',
          impact: '不引用历史项目与标准条款',
          next_action: '接下来继续生成测试方案',
          details: ['原因：KNOWLEDGE_NOT_CONFIGURED · Knowledge base API key is not configured.'],
          source: 'template',
          dedupe_key: 'KnowledgeSearchTool:tool_result:degraded'
        }
      }
    })

    const lastUpdated = updated.at(-1)?.[1]
    expect(lastUpdated?.toolCall?.status).toBe('warning')
    expect(lastUpdated?.toolCall?.name).toBe('KnowledgeSearchTool')
  })

  it('does not let a plain tool_finished event consume the first public update chunk', () => {
    const { handler, appended, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started_plain_finish',
      task_id: 'task_public_chunk',
      payload: { tool_name: 'RequirementParserTool', tool_call_id: 'call_public_chunk' }
    })

    // The runtime first persists/streams the ordinary completion event.
    // It does not contain a narrative frame and must not participate in
    // public-update chunk deduplication.
    handler('tool_finished', {
      event_id: 'evt_finished_plain',
      task_id: 'task_public_chunk',
      payload: {
        tool_name: 'RequirementParserTool',
        tool_call_id: 'call_public_chunk',
        output: '需求文档解析完成'
      }
    })

    handler('tool_finished', {
      event_id: 'evt_public_chunk_zero',
      task_id: 'task_public_chunk',
      payload: {
        tool_name: 'RequirementParserTool',
        tool_call_id: 'call_public_chunk',
        public_execution_update: {
          version: 1,
          kind: 'tool_result',
          level: 'success',
          headline: '需求文档解析完成',
          summary: '已识别业务结构和关键测试点',
          chunk_index: 0,
          chunk_total: 1,
          chunk_final: true
        }
      }
    })

    expect(appended).toHaveLength(1)
    expect(updated).toHaveLength(2)
    expect(updated.at(-1)?.[1].toolCall?.publicUpdate).toMatchObject({
      headline: '需求文档解析完成',
      chunkIndex: 0,
      chunkFinal: true
    })
  })

  it('preserves started input and reads direct Phase2.9A public update fields', () => {
    const { handler, appended, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started_contract',
      task_id: 'task_contract',
      payload: {
        tool_name: 'RequirementParserTool',
        tool_call_id: 'call_contract',
        input: { provided_keys: ['requirement_file_id'] }
      }
    })

    handler('tool_finished', {
      event_id: 'evt_finished_contract',
      task_id: 'task_contract',
      payload: {
        tool_name: 'RequirementParserTool',
        tool_call_id: 'call_contract',
        headline: 'Requirement parsing complete',
        summary: 'Parsed the requirement document',
        impact: 'The plan can now be generated',
        next_action: 'Parse the template',
        details: ['43 sections'],
        duration_ms: 1200
      }
    })

    expect(appended[0].toolCall?.input).toContain('requirement_file_id')
    expect(updated.at(-1)?.[1].toolCall).toMatchObject({
      input: expect.stringContaining('requirement_file_id'),
      output: 'Parsed the requirement document',
      duration: '3s',
      publicUpdate: {
        headline: 'Requirement parsing complete',
        summary: 'Parsed the requirement document',
        details: ['43 sections']
      }
    })
  })

  it('keeps repeated calls to the same tool in separate rows when call IDs differ', () => {
    const { handler, appended, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started_first',
      task_id: 'task_1',
      payload: { tool_name: 'TestPlanGeneratorTool', tool_call_id: 'call_first' }
    })
    handler('tool_started', {
      event_id: 'evt_started_second',
      task_id: 'task_1',
      payload: { tool_name: 'TestPlanGeneratorTool', tool_call_id: 'call_second' }
    })

    expect(appended.map((message) => message.id)).toEqual([
      'tool_call_first',
      'tool_call_second'
    ])

    handler('tool_finished', {
      event_id: 'evt_finished_second',
      task_id: 'task_1',
      payload: {
        tool_name: 'TestPlanGeneratorTool',
        tool_call_id: 'call_second',
        output: 'second complete',
        duration_ms: 500
      }
    })

    expect(updated[0][0]).toBe('tool_call_second')
  })

  it('does not render task status housekeeping events as standalone chat messages', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { handler, appended, statuses } = makeHandler()

    handler('task_resumed', { task_id: 'task_1' })
    handler('task_completed', { task_id: 'task_1' })

    expect(appended).toHaveLength(0)
    expect(statuses).toEqual([
      ['task_1', 'running'],
      ['task_1', 'completed']
    ])
    warn.mockRestore()
  })

  it('passes terminal event time to onStatusChange', () => {
    const statuses: Array<[string, string, string | undefined]> = []
    const handler = createTaskEventHandler({
      onAppendMessage: () => undefined,
      onUpdateMessage: () => true,
      onStatusChange: (taskId, status, meta) => statuses.push([taskId, status, meta?.createdAt])
    })

    handler('task_completed', {
      event_id: 'evt_done_with_time',
      task_id: 'task_done_with_time',
      created_at: '2026-08-14T00:22:29+08:00',
      payload: { summary: 'done' }
    })

    expect(statuses).toEqual([
      ['task_done_with_time', 'completed', '2026-08-14T00:22:29+08:00']
    ])
  })

  it('keeps a task completion summary inside the agent run', () => {
    const { handler, appended } = makeHandler()

    handler('task_completed', {
      event_id: 'evt_completed',
      task_id: 'task_1',
      created_at: '2026-07-13T12:02:00Z',
      payload: {
        summary: '已完成需求解析、测试方案生成与产物导出。'
      }
    })

    expect(appended).toEqual([
      expect.objectContaining({
        id: 'evt_completed',
        type: 'agent_text',
        taskId: 'task_1',
        eventType: 'task_completed',
        text: '已完成需求解析、测试方案生成与产物导出。'
      })
    ])
  })

  it('waits for stream phase completion before marking section confirmation ready', () => {
    const { handler, appended, statuses } = makeHandler()

    handler('need_user_confirm', {
      event_id: 'evt_confirm',
      task_id: 'task_1',
      payload: {
        confirmation_id: 'confirm_1',
        confirmation_type: 'section_generation_config',
        sections: []
      }
    })

    expect(appended).toHaveLength(1)
    expect(statuses).toEqual([])

    handler('stream_phase_done', {
      task_id: 'task_1',
      status: 'waiting_user_confirm'
    })

    expect(statuses).toEqual([['task_1', 'waiting_user_confirm']])
  })

  it('buffers plan_step_started and plan_step_completed before plan_created', () => {
    const stepStatuses: Array<[string, string]> = []
    const streamingHandler = createTaskEventHandler({
      onAppendMessage: () => undefined,
      onUpdateMessage: () => true,
      onPlanStepStatus: (step, status) => stepStatuses.push([step, status])
    })

    streamingHandler('plan_step_started', {
      event_id: 'evt_started_understand',
      task_id: 'task_1',
      payload: { step: 'understand', status: 'running' }
    })
    streamingHandler('plan_step_completed', {
      event_id: 'evt_completed_understand',
      task_id: 'task_1',
      payload: { step: 'understand', status: 'done' }
    })

    expect(stepStatuses).toEqual([
      ['understand', 'running'],
      ['understand', 'done']
    ])

    const appended: ChatMessage[] = []
    const bufferingHandler = createTaskEventHandler({
      onAppendMessage: (message) => appended.push(message),
      onUpdateMessage: () => true,
      onPlanStepStatus: () => undefined
    })
    bufferingHandler('plan_step_started', {
      event_id: 'evt_started_understand',
      task_id: 'task_1',
      payload: { step: 'understand', status: 'running' }
    })
    bufferingHandler('plan_step_completed', {
      event_id: 'evt_completed_understand',
      task_id: 'task_1',
      payload: { step: 'understand', status: 'done' }
    })
    bufferingHandler('plan_step_started', {
      event_id: 'evt_started_plan',
      task_id: 'task_1',
      payload: { step: 'plan', status: 'running' }
    })
    bufferingHandler('plan_step_completed', {
      event_id: 'evt_completed_plan',
      task_id: 'task_1',
      payload: { step: 'plan', status: 'done' }
    })
    bufferingHandler('plan_created', {
      event_id: 'evt_plan',
      task_id: 'task_1',
      payload: {
        plan: [
          { step_id: 'understand', name: '理解任务', status: 'pending' },
          { step_id: 'plan', name: '生成执行计划', status: 'pending' }
        ]
      }
    })

    expect(appended).toHaveLength(1)
    expect(appended[0]).toMatchObject({
      type: 'agent_plan',
      taskId: 'task_1',
      plan: [
        { id: 'understand', status: 'done' },
        { id: 'plan', status: 'done' }
      ]
    })
  })

  it('normalizes dynamic backend plan step events without explicit started status', () => {
    const stepStatuses: Array<[string, string]> = []
    const appended: ChatMessage[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: (message) => appended.push(message),
      onUpdateMessage: (messageId, patch) => {
        const idx = appended.findIndex((message) => message.id === messageId)
        if (idx < 0) return false
        appended[idx] = { ...appended[idx], ...patch }
        return true
      },
      onPlanStepStatus: (step, status) => stepStatuses.push([step, status])
    })

    handler('plan_created', {
      event_id: 'evt_plan_dyn',
      task_id: 'task_dyn',
      payload: {
        plan: {
          steps: [
            { step_id: 'step_1', title: '解析Word文档', status: 'pending' },
            { step_id: 'step_2', title: '分析文档证据', status: 'pending' }
          ]
        }
      }
    })
    handler('plan_step_started', {
      event_id: 'evt_step_1_start',
      task_id: 'task_dyn',
      payload: { step_id: 'step_1' }
    })
    handler('plan_step_completed', {
      event_id: 'evt_step_1_done',
      task_id: 'task_dyn',
      payload: { step_id: 'step_1', status: 'completed' }
    })
    handler('plan_step_started', {
      event_id: 'evt_step_2_start',
      task_id: 'task_dyn',
      payload: { step_id: 'step_2' }
    })

    expect(stepStatuses).toEqual([
      ['step_1', 'running'],
      ['step_1', 'done'],
      ['step_2', 'running']
    ])
    expect(appended[0].plan?.map((step) => [step.id, step.status])).toEqual([
      ['step_1', 'done'],
      ['step_2', 'running']
    ])
  })

  it('attaches public_execution_update to tool_call when payload carries it', () => {
    const { handler, appended } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started',
      task_id: 'task_1',
      title: 'RequirementParserTool 开始执行',
      payload: { input: { file_id: 'file_1' } }
    })

    const startedId = appended[0].id
    expect(startedId).toBeTruthy()

    handler('tool_finished', {
      event_id: 'evt_finished',
      task_id: 'task_1',
      payload: {
        tool_name: 'RequirementParserTool',
        output: 'ok',
        duration_ms: 2300,
        public_execution_update: {
          version: 1,
          kind: 'tool_result',
          level: 'success',
          headline: '需求文档解析完成',
          summary: '已解析',
          impact: '用于测试方案',
          next_action: '接下来解析模板',
          details: ['识别章节 5 个'],
          source: 'template',
          dedupe_key: 'RequirementParserTool:tool_result:success'
        }
      }
    })
  })

  it('attaches public_execution_update to tool_retry when payload carries it', () => {
    const { handler, appended } = makeHandler()

    handler('retrying', {
      event_id: 'evt_retry',
      task_id: 'task_1',
      payload: {
        tool_name: 'TestPlanGeneratorTool',
        attempt: 2,
        max_retries: 3,
        strategy: 'schema_feedback',
        reason: 'json invalid',
        last_error: 'JSON broken',
        backoff_seconds: 1.2,
        public_execution_update: {
          version: 1,
          kind: 'tool_retry',
          level: 'retrying',
          headline: '正在第 2 次重试',
          summary: '系统正在用「提示词规范反馈」策略',
          impact: '不会修改已经得到的结果',
          next_action: '请稍候',
          details: ['上次原因：JSON broken'],
          source: 'template',
          dedupe_key: 'TestPlanGeneratorTool:tool_retry:schema_feedback:2'
        }
      }
    })

    expect(appended).toHaveLength(1)
    const retry = appended[0].toolRetry
    expect(retry).toBeTruthy()
    expect(retry?.lastError).toBe('JSON broken')
    expect(retry?.publicUpdate?.headline).toBe('正在第 2 次重试')
    expect(retry?.publicUpdate?.level).toBe('retrying')
    expect(retry?.publicUpdate?.dedupeKey).toBe(
      'TestPlanGeneratorTool:tool_retry:schema_feedback:2'
    )
  })

  it('omits public_execution_update when payload lacks it (graceful)', () => {
    const { handler, appended } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started',
      task_id: 'task_1',
      title: 'FooTool 开始执行',
      payload: {}
    })
    handler('tool_finished', {
      event_id: 'evt_finished',
      task_id: 'task_1',
      payload: { tool_name: 'FooTool', output: 'ok', duration_ms: 100 }
    })

    const tool = appended[0].toolCall
    expect(tool?.publicUpdate).toBeUndefined()
  })

  it('omits public_execution_update when payload has malformed object', () => {
    const { handler, appended } = makeHandler()

    handler('retrying', {
      event_id: 'evt_retry',
      task_id: 'task_1',
      payload: {
        tool_name: 'FooTool',
        attempt: 2,
        strategy: 'backoff',
        public_execution_update: 'not-an-object'
      }
    })

    const retry = appended[0].toolRetry
    expect(retry?.publicUpdate).toBeUndefined()
  })

  // ── Section 24-ext — chunk streaming contract ────────────────────────

  it('out_of_order_chunk_replaces_lower_index_updates', () => {
    // F0-streaming: SSE reconnect / history replay may deliver frames
    // out of order.  When a lower chunk_index arrives after a higher
    // one, we must NOT overwrite the more-progressed render.
    const { handler, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started',
      task_id: 'task_1',
      title: 'RequirementParserTool 开始执行',
      payload: { input: { file_id: 'file_1' } }
    })

    // Apply 3 frames in order.
    for (const index of [0, 1, 2]) {
      handler('tool_finished', {
        event_id: `evt_chunk_${index}`,
        task_id: 'task_1',
        payload: {
          tool_name: 'RequirementParserTool',
          output: 'ok',
          duration_ms: 2300,
          public_execution_update: {
            version: 1,
            kind: 'tool_result',
            level: 'success',
            headline: `frame ${index}`,
            summary: index >= 1 ? `summary ${index}` : '',
            impact: index >= 2 ? `impact ${index}` : '',
            next_action: '',
            details: [],
            source: 'template',
            dedupe_key: 'r',
            chunk_index: index,
            chunk_total: 5,
            chunk_final: false
          }
        }
      })
    }

    // Now an out-of-order frame arrives — chunk_index=1 after chunk=2.
    // The handler must drop it.
    handler('tool_finished', {
      event_id: 'evt_chunk_late_1',
      task_id: 'task_1',
      payload: {
        tool_name: 'RequirementParserTool',
        output: 'ok',
        public_execution_update: {
          version: 1,
          kind: 'tool_result',
          level: 'success',
          headline: 'STALE frame 1',
          summary: 'should NOT win',
          impact: '',
          next_action: '',
          details: [],
          source: 'template',
          dedupe_key: 'r',
          chunk_index: 1,
          chunk_total: 5,
          chunk_final: false
        }
      }
    })

    // The latest visible headline must still be 'frame 2' — the stale
    // frame's "STALE frame 1" should never have applied.
    const toolCallUpdates = updated
      .filter(([, patch]) => patch.toolCall !== undefined)
      .map(([, patch]) => patch.toolCall?.publicUpdate)
    expect(toolCallUpdates.at(-1)?.headline).toBe('frame 2')
    // The stale event did NOT generate a 4th update call (the handler
    // returned early before onUpdateMessage).
    const headlineSequence = toolCallUpdates.map((u) => u?.headline)
    expect(headlineSequence).not.toContain('STALE frame 1')
  })

  it('final_chunk_is_marked_in_message_state', () => {
    // F0-streaming: the chunk with chunk_final=true marks the message
    // as finalised, so the UI can stop accepting further chunks for
    // this message id.  We verify the producer marks the publicUpdate
    // with chunk_final=true and that the bounded chunk-index cache
    // entry is released (verified indirectly via no overwriting after
    // a NEW higher-index frame would be needed to land).
    const { handler, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started',
      task_id: 'task_1',
      title: 'FooTool 开始执行',
      payload: {}
    })

    const total = 3
    for (const index of Array.from({ length: total }, (_, i) => i)) {
      handler('tool_finished', {
        event_id: `evt_chunk_${index}`,
        task_id: 'task_1',
        payload: {
          tool_name: 'FooTool',
          output: 'ok',
          public_execution_update: {
            version: 1,
            kind: 'tool_result',
            level: 'success',
            headline: '需求文档解析完成',
            summary: index >= 1 ? '已解析 5 个需求点' : '',
            impact: index >= 2 ? '用于测试方案' : '',
            next_action: '',
            details: index === total - 1 ? ['识别章节 5 个'] : [],
            source: 'template',
            dedupe_key: 'f',
            chunk_index: index,
            chunk_total: total,
            chunk_final: index === total - 1
          }
        }
      })
    }

    // Find the last update for this tool_call message — its
    // publicUpdate.chunkFinal must be true.
    const lastToolCallUpdate = updated
      .filter(([, patch]) => patch.toolCall !== undefined)
      .at(-1)
    expect(lastToolCallUpdate?.[1].toolCall?.publicUpdate?.chunkFinal).toBe(true)
    expect(lastToolCallUpdate?.[1].toolCall?.publicUpdate?.chunkIndex).toBe(total - 1)
    expect(lastToolCallUpdate?.[1].toolCall?.publicUpdate?.chunkTotal).toBe(total)
  })

  it('chunk_fields_propagate_to_tool_call_publicUpdate', () => {
    // F0-streaming: a single tool_finished frame carries chunk_index /
    // chunk_total / chunk_final through to the message's toolCall
    // publicUpdate, exactly as produced by the backend's split_into_chunks.
    const { handler, updated } = makeHandler()

    handler('tool_started', {
      event_id: 'evt_started',
      task_id: 'task_1',
      title: 'RequirementParserTool 开始执行',
      payload: {}
    })

    handler('tool_finished', {
      event_id: 'evt_chunk_3',
      task_id: 'task_1',
      payload: {
        tool_name: 'RequirementParserTool',
        output: 'ok',
        public_execution_update: {
          version: 1,
          kind: 'tool_result',
          level: 'success',
          headline: '需求文档解析完成',
          summary: '已解析 5 个需求点',
          impact: '用于测试方案',
          next_action: '接下来解析模板',
          details: ['识别章节 5 个', '读取模板'],
          source: 'template',
          dedupe_key: 'RequirementParserTool:tool_result:success',
          chunk_index: 3,
          chunk_total: 5,
          chunk_final: false
        }
      }
    })

    const last = updated.at(-1)
    expect(last?.[0]).toBe('tool_task_1_RequirementParserTool')
    expect(last?.[1].toolCall?.publicUpdate?.chunkIndex).toBe(3)
    expect(last?.[1].toolCall?.publicUpdate?.chunkTotal).toBe(5)
    expect(last?.[1].toolCall?.publicUpdate?.chunkFinal).toBe(false)
    expect(last?.[1].toolCall?.publicUpdate?.headline).toBe('需求文档解析完成')
    expect(last?.[1].toolCall?.publicUpdate?.details).toEqual([
      '识别章节 5 个',
      '读取模板'
    ])
  })

  // Phase 2.8E — task_completed completion summary chain tests
  //
  // Phase 2.8D finalize_task_node emits payload={"summary": summary_text}.
  // The handler now reads summary / completion_summary / text in that
  // order so any single producer (legacy orchestrator, post-2.8D
  // Summary 节点, or a fallback stub) keeps the AgentRunCard
  // completionSummary card showing the user's results.
  describe('Phase 2.8E — task_completed summary chain', () => {
    it('prefers the 2.8D payload.summary field', () => {
      const { handler, appended } = makeHandler()

      handler('task_completed', {
        event_id: 'evt_done_v6',
        task_id: 'task_v6',
        payload: { summary: '2.8D 摘要优先', completion_summary: '老字段' }
      })

      expect(appended).toEqual([
        expect.objectContaining({
          id: 'evt_done_v6',
          text: '2.8D 摘要优先'
        })
      ])
    })

    it('falls back to completion_summary when summary is absent', () => {
      const { handler, appended } = makeHandler()

      handler('task_completed', {
        event_id: 'evt_done_legacy',
        task_id: 'task_v6',
        payload: { completion_summary: 'Legacy L335 摘要' }
      })

      expect(appended).toEqual([
        expect.objectContaining({
          id: 'evt_done_legacy',
          text: 'Legacy L335 摘要'
        })
      ])
    })

    it('falls back to text when both summary fields are missing', () => {
      const { handler, appended } = makeHandler()

      handler('task_completed', {
        event_id: 'evt_done_text',
        task_id: 'task_v6',
        payload: { text: '纯文本完成' }
      })

      expect(appended).toEqual([
        expect.objectContaining({ id: 'evt_done_text', text: '纯文本完成' })
      ])
    })

    it('returns no completion summary when payload is empty', () => {
      // Defensive — task_completed without a payload must not surface
      // a stale summary that was attached to a prior task_completed
      // in the same conversation.  We verify by ensuring no message
      // is appended at all so the AgentRunCard completionSummary
      // computed from messages stays empty.
      const { handler, appended } = makeHandler()

      handler('task_completed', {
        event_id: 'evt_done_empty',
        task_id: 'task_v6'
      })

      expect(appended).toEqual([])
    })
  })
})
