import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AgentTask, Artifact, ChatMessage, Conversation, FileAttachment } from '@/types'

vi.mock('@/api/conversationApi', () => ({
  fetchConversations: vi.fn(),
  fetchConversationDetail: vi.fn(),
  createConversation: vi.fn(),
  updateConversationTitle: vi.fn(),
  deleteConversation: vi.fn(),
  fetchConversationContextSettings: vi.fn(),
  updateConversationKnowledgeMode: vi.fn()
}))

vi.mock('@/api/messageApi', () => ({
  fetchMessages: vi.fn()
}))

vi.mock('@/api/fileApi', () => ({
  fetchConversationFiles: vi.fn()
}))

vi.mock('@/api/agentApi', () => ({
  fetchTaskDetail: vi.fn(),
  fetchTaskEvents: vi.fn(),
  fetchAllTaskEvents: vi.fn(),
  fetchPendingConfirmation: vi.fn(),
  fetchConfirmedConfirmations: vi.fn()
}))

vi.mock('@/api/artifactApi', () => ({
  fetchTaskArtifacts: vi.fn()
}))

import * as conversationApi from '@/api/conversationApi'
import * as messageApi from '@/api/messageApi'
import * as fileApi from '@/api/fileApi'
import * as agentApi from '@/api/agentApi'
import { useConversationStore } from './conversationStore'
import { assembleConversationTimeline } from '@/utils/conversationTimeline'

const task: AgentTask = {
  task_id: 'task_001',
  task_type: 'test_plan_generation',
  status: 'running',
  events_url: '/api/agent/tasks/task_001/events'
}

const conversationDetail: Conversation = {
  id: 'conv_001',
  title: 'history',
  subtitle: '',
  state: 'planning',
  updatedAt: '2026-06-19T21:35:00+08:00',
  latestTask: task,
  files: [],
  draftFiles: [],
  messages: []
}

function message(id: string, role: ChatMessage['role'], text: string, createdAt: string): ChatMessage {
  return {
    id,
    role,
    type: role === 'user' ? 'user_text' : 'agent_text',
    text,
    timestamp: createdAt,
    createdAt
  }
}

function conversation(overrides: Partial<Conversation>): Conversation {
  return {
    id: 'conv_base',
    title: 'history',
    subtitle: '',
    state: 'uploaded',
    updatedAt: '2026-08-01T08:00:00+00:00',
    latestTask: null,
    tasks: [],
    files: [],
    draftFiles: [],
    messages: [],
    ...overrides
  }
}

describe('conversationStore history restore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(conversationApi.fetchConversations).mockResolvedValue([])
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue(conversationDetail)
    vi.mocked(conversationApi.fetchConversationContextSettings).mockResolvedValue({
      memory_mode: 'inherit',
      knowledge_mode: 'AUTO',
      context_workspace_key: 'conversation:conv_001',
      context_engine_version: 'v3',
      policy_version: '2026-08-09'
    })
    vi.mocked(conversationApi.updateConversationKnowledgeMode).mockImplementation(async (_conversationId, mode) => ({
      knowledge_mode: mode
    }))
    vi.mocked(fileApi.fetchConversationFiles).mockResolvedValue([])
    vi.mocked(agentApi.fetchTaskDetail).mockResolvedValue(task)
    const apiWithConfirmationHistory = agentApi as unknown as {
      fetchConfirmedConfirmations: ReturnType<typeof vi.fn>
    }
    apiWithConfirmationHistory.fetchConfirmedConfirmations.mockResolvedValue([])
    vi.mocked(agentApi.fetchAllTaskEvents).mockResolvedValue([
      {
        event_id: 'evt_plan',
        task_id: 'task_001',
        event_type: 'plan_created',
        created_at: '2026-06-19T21:31:00+08:00',
        payload: {
          steps: [{ step_id: 'step_1', name: 'Parse requirement', status: 'done' }]
        }
      }
    ])
  })

  it('places task events in taskRunBlocks, not in messages', async () => {
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([
      message('msg_user_task', 'user', 'generate test plan', '2026-06-19T21:30:00+08:00'),
      message('msg_user_followup', 'user', 'analyze this error log', '2026-06-19T21:32:00+08:00'),
      message('msg_agent_followup', 'agent', 'please paste the error log', '2026-06-19T21:33:00+08:00')
    ])

    const store = useConversationStore()
    const restored = await store.restoreConversation('conv_001')

    // Phase 2.9A.30: messages only contains regular messages (no task events)
    expect(restored.messages.map((item) => item.id)).toEqual([
      'msg_user_task',
      'msg_user_followup',
      'msg_agent_followup'
    ])
    // Task events are in taskRunBlocks
    expect(restored.taskRunBlocks).toHaveLength(1)
    expect(restored.taskRunBlocks![0].messages.some(m => m.type === 'agent_plan')).toBe(true)
  })

  it('preserves optimistic messages when a stale restore snapshot races with send', async () => {
    const pendingUser: ChatMessage = {
      id: 'msg_user_pending_1',
      role: 'user',
      type: 'user_text',
      text: '项目概述章节的内容太少了，需要更加丰富，起码达到100字才可以',
      clientOrder: 2,
      timelineRank: 0,
      timestamp: '22:53'
    }
    const pendingThinking: ChatMessage = {
      id: 'msg_agent_thinking_1',
      role: 'agent',
      type: 'agent_text',
      text: '正在思考...',
      thinking: true,
      streaming: true,
      clientOrder: 2,
      timelineRank: 1,
      anchorMessageId: pendingUser.id,
      timestamp: '22:53'
    }

    const store = useConversationStore()
    await Promise.resolve()
    store.conversations = [conversation({
      id: 'conv_001',
      messages: [pendingUser, pendingThinking],
      state: 'planning'
    })]
    store.setActiveConversation('conv_001')

    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue(
      conversation({ id: 'conv_001', state: 'planning' })
    )
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([])
    vi.mocked(fileApi.fetchConversationFiles).mockResolvedValue([])

    const restored = await store.restoreConversation('conv_001')

    expect(restored.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: pendingUser.id,
        text: pendingUser.text
      }),
      expect.objectContaining({
        id: pendingThinking.id,
        thinking: true
      })
    ]))
  })

  it('keeps the history restore indicator active until the newest restore finishes', async () => {
    let resolveFirst: ((value: Conversation) => void) | undefined
    let resolveSecond: ((value: Conversation) => void) | undefined
    vi.mocked(conversationApi.fetchConversationDetail).mockImplementation((id) =>
      new Promise<Conversation>((resolve) => {
        if (id === 'conv_first') resolveFirst = resolve
        else resolveSecond = resolve
      })
    )
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([])
    vi.mocked(fileApi.fetchConversationFiles).mockResolvedValue([])

    const store = useConversationStore()
    const firstRestore = store.restoreConversation('conv_first')
    const secondRestore = store.restoreConversation('conv_second')
    expect(store.restoringConversation).toBe(true)

    resolveFirst?.(conversation({ id: 'conv_first' }))
    await firstRestore
    expect(store.restoringConversation).toBe(true)

    resolveSecond?.(conversation({ id: 'conv_second' }))
    await secondRestore
    expect(store.restoringConversation).toBe(false)
  })

  it('preserves draft attachments when restoring a newly created conversation', async () => {
    const draftAttachment = {
      id: 'file_library_001',
      name: '测试方案.docx',
      size: '28 KB',
      extension: '.docx',
      type: 'supplemental_doc' as const,
      status: 'uploaded' as const
    }
    const store = useConversationStore()
    await Promise.resolve()
    store.conversations = [conversation({
      id: 'conv_001',
      draftFiles: [draftAttachment]
    })]

    const restored = await store.restoreConversation('conv_001')

    expect(restored.draftFiles).toEqual([draftAttachment])
    expect(store.activeConversation?.draftFiles).toEqual([draftAttachment])
  })

  it('preserves a reconciled user message when restore finishes after message_created', async () => {
    const pendingUser: ChatMessage = {
      id: 'msg_user_pending_2',
      role: 'user',
      type: 'user_text',
      text: '当前用户消息',
      clientOrder: 3,
      timelineRank: 0,
      timestamp: '22:54'
    }
    const pendingThinking: ChatMessage = {
      id: 'msg_agent_thinking_2',
      role: 'agent',
      type: 'agent_text',
      text: '正在思考...',
      thinking: true,
      streaming: true,
      clientOrder: 3,
      timelineRank: 1,
      anchorMessageId: pendingUser.id,
      timestamp: '22:54'
    }

    const store = useConversationStore()
    await Promise.resolve()
    store.conversations = [conversation({
      id: 'conv_001',
      messages: [pendingUser, pendingThinking],
      state: 'planning'
    })]
    store.setActiveConversation('conv_001')
    store.updateMessage('conv_001', pendingUser.id, Object.assign({
      id: 'msg_server_user_2',
      conversationSequence: 5
    }, { optimistic: true }))

    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue(
      conversation({ id: 'conv_001', state: 'planning' })
    )
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([])
    vi.mocked(fileApi.fetchConversationFiles).mockResolvedValue([])

    const restored = await store.restoreConversation('conv_001')

    expect(restored.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'msg_server_user_2', text: '当前用户消息' }),
      expect.objectContaining({ id: pendingThinking.id, thinking: true })
    ]))
  })

  it('restores the completed task timeline and artifacts in taskRunBlocks', async () => {
    const completedTask: AgentTask = { ...task, status: 'completed' }
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      latestTask: completedTask
    })
    vi.mocked(agentApi.fetchTaskDetail).mockResolvedValue(completedTask)
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([
      message('msg_user_task', 'user', 'generate test plan', '2026-06-19T21:30:00+08:00')
    ])
    vi.mocked(agentApi.fetchAllTaskEvents).mockResolvedValue([
      {
        event_id: 'evt_plan',
        task_id: 'task_001',
        event_type: 'plan_created',
        created_at: '2026-06-19T21:31:00+08:00',
        payload: {
          steps: [{ step_id: 'step_1', name: 'Parse requirement', status: 'done' }]
        }
      }
    ])
    const artifact: Artifact = {
      id: 'artifact_001',
      type: 'test_plan_word',
      name: 'test-plan.docx',
      size: '120 KB',
      generatedAt: '2026-06-19T21:34:00+08:00'
    }
    const artifactApi = await import('@/api/artifactApi')
    vi.mocked(artifactApi.fetchTaskArtifacts).mockResolvedValue([artifact])

    const store = useConversationStore()
    const restored = await store.restoreConversation('conv_001')

    // Phase 2.9A.30: task events and artifacts are in taskRunBlocks
    expect(restored.taskRunBlocks).toHaveLength(1)
    const block = restored.taskRunBlocks![0]
    expect(block.messages.some(m => m.type === 'agent_plan')).toBe(true)
    expect(block.artifacts).toEqual([artifact])
    expect(artifactApi.fetchTaskArtifacts).toHaveBeenCalledWith('task_001')
  })

  it('does not restore an actionable section confirmation after the task has resumed', async () => {
    const completedTask: AgentTask = { ...task, status: 'completed' }
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      latestTask: completedTask
    })
    vi.mocked(agentApi.fetchTaskDetail).mockResolvedValue(completedTask)
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([
      message('msg_user_task', 'user', 'generate test plan', '2026-06-19T21:30:00+08:00')
    ])
    vi.mocked(agentApi.fetchAllTaskEvents).mockResolvedValue([
      {
        event_id: 'evt_confirm',
        task_id: 'task_001',
        event_type: 'need_user_confirm',
        sequence_no: 3,
        created_at: '2026-06-19T21:34:00+08:00',
        payload: {
          confirmation_id: 'confirm_001',
          confirmation_type: 'section_generation_config',
          sections: []
        }
      },
      {
        event_id: 'evt_plan',
        task_id: 'task_001',
        event_type: 'plan_created',
        sequence_no: 2,
        created_at: '2026-06-19T21:35:00+08:00',
        payload: {
          steps: [{ step_id: 'step_1', name: 'Parse requirement', status: 'done' }]
        }
      },
      {
        event_id: 'evt_resumed',
        task_id: 'task_001',
        event_type: 'task_resumed',
        sequence_no: 4,
        created_at: '2026-06-19T21:33:00+08:00',
        payload: { text: 'sections confirmed' }
      }
    ])

    const store = useConversationStore()
    const restored = await store.restoreConversation('conv_001')

    // Phase 2.9A.30: task events are in taskRunBlocks
    expect(restored.taskRunBlocks).toHaveLength(1)
    const block = restored.taskRunBlocks![0]
    // plan_created should be present
    expect(block.messages.some(m => m.type === 'agent_plan')).toBe(true)
    // section_confirm should NOT be present (task is completed, not waiting)
    expect(block.messages.some(m => m.type === 'section_confirm')).toBe(false)
  })

  it('restores persisted confirmation receipts after a page refresh', async () => {
    const completedTask: AgentTask = { ...task, status: 'completed' }
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      latestTask: completedTask
    })
    vi.mocked(agentApi.fetchTaskDetail).mockResolvedValue(completedTask)
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([])
    vi.mocked(agentApi.fetchAllTaskEvents).mockResolvedValue([
      {
        event_id: 'evt_confirm',
        task_id: 'task_001',
        event_type: 'need_user_confirm',
        canonical_order: 8,
        created_at: '2026-06-19T21:34:00+08:00',
        payload: {
          confirmation_id: 'confirm_001',
          confirmation_type: 'section_generation_config'
        }
      },
      {
        event_id: 'evt_resumed',
        task_id: 'task_001',
        event_type: 'task_resumed',
        canonical_order: 9,
        created_at: '2026-06-19T21:35:00+08:00'
      }
    ])
    const apiWithConfirmationHistory = agentApi as unknown as {
      fetchConfirmedConfirmations: ReturnType<typeof vi.fn>
    }
    apiWithConfirmationHistory.fetchConfirmedConfirmations.mockResolvedValue([
      {
        confirmationId: 'confirm_001',
        confirmationType: 'section_generation_config',
        request: { sections: [{ id: 'overview', code: '1', title: '项目概述', level: 'H1', suggestedAction: 'ai_generate', action: 'ai_generate', reason: '' }] },
        response: { sections: [{ id: 'overview', code: '1', title: '项目概述', level: 'H1', suggestedAction: 'ai_generate', action: 'ai_generate', reason: '' }] },
        confirmedAt: '2026-06-19T21:35:00+08:00'
      }
    ])

    const store = useConversationStore()
    const restored = await store.restoreConversation('conv_001')
    const receipt = restored.taskRunBlocks?.[0].messages.find(
      (item) => item.confirmationReceipt?.kind === 'section'
    )

    expect(receipt).toEqual(expect.objectContaining({
      id: 'confirmation_receipt_confirm_001',
      confirmationId: 'confirm_001',
      conversationSequence: 8.5,
      confirmationReceipt: expect.objectContaining({
        markdown: expect.stringContaining('项目概述')
      })
    }))
  })

  // Phase 2.9A.27 ──────────────────────────────────────────────────
  // 新增: completed task 在 hydrate 完成后 latestTask.status 应为 'completed'
  // 即便 task detail 返回 'completed' 而事件流有 task_completed 事件。

  it('sets latestTask.status to completed when task_completed event present', async () => {
    const completedTask: AgentTask = { ...task, status: 'completed', startedAt: '2026-07-29T04:50:50Z', completedAt: '2026-07-29T04:53:55Z' }
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      latestTask: completedTask
    })
    vi.mocked(agentApi.fetchTaskDetail).mockResolvedValue(completedTask)
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([
      message('msg_user_task', 'user', '生成测试方案', '2026-07-29T04:50:38Z')
    ])
    vi.mocked(agentApi.fetchAllTaskEvents).mockResolvedValue([
      {
        event_id: 'evt_plan',
        task_id: 'task_001',
        event_type: 'plan_created',
        canonical_order: 5,
        created_at: '2026-07-29T04:51:00Z',
        payload: { steps: [{ step_id: 'step_1', name: '理解', status: 'done' }] }
      },
      {
        event_id: 'evt_tool',
        task_id: 'task_001',
        event_type: 'tool_finished',
        canonical_order: 10,
        created_at: '2026-07-29T04:52:00Z',
        payload: { tool_call_id: 'tc_1', tool_name: 'RequirementParserTool', chunk_final: true, publicUpdate: { version: 1, kind: 'tool_result', level: 'success', headline: 'Done', summary: '', impact: '', nextAction: '', details: [], source: 'template', dedupeKey: 'tc_1', chunkIndex: 4, chunkTotal: 5, chunkFinal: true } }
      },
      {
        event_id: 'evt_complete',
        task_id: 'task_001',
        event_type: 'task_completed',
        canonical_order: 59,
        created_at: '2026-07-29T04:53:55Z',
        content: '任务已完成。已生成 16 个章节。',
        payload: {
          summary_facts: {
            generated_sections: 16,
            kept_sections: 1,
            business_modules: 0,
            review: { block_count: 3, warning_count: 0, suggestion_count: 1, level: 'warning', passed: true },
            artifact: { present: true, file_name: '方案.docx', format_status: 'passed' }
          }
        }
      }
    ])

    const store = useConversationStore()
    const restored = await store.restoreConversation('conv_001')

    expect(restored.latestTask?.status).toBe('completed')
    // Phase 2.9A.30: task events are in taskRunBlocks
    expect(restored.taskRunBlocks).toHaveLength(1)
    const block = restored.taskRunBlocks![0]
    expect(block.status).toBe('completed')
    // task_completed message should exist in block
    const completedMsg = block.messages.find((m) => m.eventType === 'task_completed')
    expect(completedMsg).toBeTruthy()
    expect(completedMsg?.type).toBe('agent_text')
  })

  it('task block status updates to completed when task_completed event present', async () => {
    // task detail says running, but events have task_completed → reducer updates block status
    const staleTask: AgentTask = { ...task, status: 'running' }
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      latestTask: staleTask
    })
    vi.mocked(agentApi.fetchTaskDetail).mockResolvedValue(staleTask)
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([])
    vi.mocked(agentApi.fetchAllTaskEvents).mockResolvedValue([
      {
        event_id: 'evt_tc',
        task_id: 'task_001',
        event_type: 'task_completed',
        canonical_order: 59,
        created_at: '2026-07-29T04:53:55Z',
        content: '任务已完成',
        payload: {}
      }
    ])

    const store = useConversationStore()
    const restored = await store.restoreConversation('conv_001')

    // Phase 2.9A.30: block status is updated by reducer
    expect(restored.taskRunBlocks).toHaveLength(1)
    expect(restored.taskRunBlocks![0].status).toBe('completed')
  })

  it('hydrates full conversation tasks even when latestTask is only a summary', async () => {
    const taskSummary: AgentTask = {
      task_id: 'task_7e15d3a4',
      task_type: 'test_plan_generation',
      status: 'completed',
      events_url: '/api/agent/tasks/task_7e15d3a4/events',
      triggerMessageId: null
    }
    const fullTask: AgentTask = {
      ...taskSummary,
      triggerMessageId: 'msg_3129a663',
      startedAt: '2026-08-01T04:58:45+00:00',
      completedAt: '2026-08-01T05:01:34+00:00',
      durationMs: 169000
    }
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_efd5c72e',
      latestTask: taskSummary,
      tasks: [fullTask]
    })
    vi.mocked(agentApi.fetchTaskDetail).mockResolvedValue(fullTask)
    vi.mocked(messageApi.fetchMessages).mockResolvedValue([
      {
        ...message('msg_3129a663', 'user', '帮我生成测试方案', '2026-08-01T04:58:40+00:00'),
        conversationSequence: 1
      },
      {
        ...message('msg_weather', 'user', '今天天气怎么样', '2026-08-01T05:02:00+00:00'),
        conversationSequence: 2
      }
    ])
    vi.mocked(agentApi.fetchAllTaskEvents).mockResolvedValue([])

    const store = useConversationStore()
    const restored = await store.restoreConversation('conv_efd5c72e')

    expect(restored.tasks).toEqual([
      expect.objectContaining({
        task_id: 'task_7e15d3a4',
        triggerMessageId: 'msg_3129a663',
        startedAt: '2026-08-01T04:58:45+00:00',
        completedAt: '2026-08-01T05:01:34+00:00',
        durationMs: 169000
      })
    ])
    expect(restored.latestTask).toEqual(
      expect.objectContaining({
        task_id: 'task_7e15d3a4',
        durationMs: 169000
      })
    )
    expect(restored.taskRunBlocks?.[0]).toEqual(
      expect.objectContaining({
        triggerMessageId: 'msg_3129a663',
        durationMs: 169000,
        collapsed: true
      })
    )
  })

  it('anchors live task blocks immediately without requiring conversation reload', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_live',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_live')
    store.setActiveConversation('conv_live')
    store.appendMessages('conv_live', [
      {
        ...message('msg_task_request', 'user', '帮我生成测试方案', '2026-08-01T04:58:40+00:00'),
        conversationSequence: 1
      },
      {
        ...message('msg_hello', 'user', '你好', '2026-08-01T05:02:00+00:00'),
        conversationSequence: 2
      },
      {
        ...message('msg_hello_reply', 'agent', '你好！', '2026-08-01T05:02:01+00:00'),
        conversationSequence: 3
      }
    ])

    store.setLatestTask('conv_live', {
      task_id: 'task_live',
      task_type: 'test_plan_generation',
      status: 'created',
      events_url: '/api/agent/tasks/task_live/events',
      triggerMessageId: 'msg_task_request'
    })
    store.applyLiveTaskEvent('conv_live', 'task_live', {
      event_id: 'evt_task_created',
      task_id: 'task_live',
      event_type: 'task_created',
      created_at: '2026-08-01T04:58:45+00:00',
      payload: {}
    })

    const conversation = store.activeConversation
    const items = assembleConversationTimeline(
      conversation.messages,
      conversation.taskRunBlocks ?? [],
      conversation.tasks ?? []
    )

    expect(conversation.tasks?.[0]).toEqual(
      expect.objectContaining({
        task_id: 'task_live',
        triggerMessageId: 'msg_task_request'
      })
    )
    expect(conversation.taskRunBlocks?.[0]).toEqual(
      expect.objectContaining({
        taskId: 'task_live',
        triggerMessageId: 'msg_task_request'
      })
    )
    expect(items.map((item) => item.id)).toEqual([
      'msg_task_request',
      'run_task_live',
      'msg_hello',
      'msg_hello_reply'
    ])
  })

  it('removes all stale agent thinking placeholders for a task conversation', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_thinking',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_thinking')
    store.setActiveConversation('conv_thinking')
    store.appendMessages('conv_thinking', [
      {
        ...message('msg_task_request', 'user', 'generate test plan', '2026-08-01T04:58:40+00:00'),
        conversationSequence: 1
      },
      {
        ...message('msg_thinking_1', 'agent', '正在思考...', '2026-08-01T04:58:41+00:00'),
        thinking: true,
        streaming: true,
        anchorMessageId: 'msg_task_request'
      },
      {
        ...message('msg_thinking_2', 'agent', '正在思考...', '2026-08-01T04:58:42+00:00'),
        thinking: true,
        streaming: true,
        anchorMessageId: 'msg_task_request'
      },
      {
        ...message('msg_regular_agent', 'agent', 'keep me', '2026-08-01T04:58:43+00:00'),
        thinking: false,
        streaming: false
      }
    ])

    const removed = store.removeTaskThinkingPlaceholders('conv_thinking', 'msg_task_request')

    expect(removed).toBe(2)
    expect(store.activeConversation.messages.map((item) => item.id)).toEqual([
      'msg_task_request',
      'msg_regular_agent'
    ])
  })

  it('removes stale thinking placeholders when called with the placeholder message id', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_thinking_by_placeholder',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_thinking_by_placeholder')
    store.setActiveConversation('conv_thinking_by_placeholder')
    store.appendMessages('conv_thinking_by_placeholder', [
      {
        ...message('msg_task_request', 'user', 'generate test plan', '2026-08-01T04:58:40+00:00'),
        conversationSequence: 1
      },
      {
        ...message('msg_thinking_1', 'agent', '正在思考...', '2026-08-01T04:58:41+00:00'),
        thinking: true,
        streaming: true,
        anchorMessageId: 'msg_task_request'
      },
      {
        ...message('msg_thinking_2', 'agent', '正在思考...', '2026-08-01T04:58:42+00:00'),
        thinking: true,
        streaming: true,
        anchorMessageId: 'msg_task_request'
      }
    ])

    const removed = store.removeTaskThinkingPlaceholders('conv_thinking_by_placeholder', 'msg_thinking_1')

    expect(removed).toBe(2)
    expect(store.activeConversation.messages.map((item) => item.id)).toEqual(['msg_task_request'])
  })

  it('updates messages stored inside task run blocks', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_task_message_update',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_task_message_update')
    store.setActiveConversation('conv_task_message_update')
    store.applyLiveTaskEvent('conv_task_message_update', 'task_waiting', {
      event_id: 'evt_confirm',
      task_id: 'task_waiting',
      event_type: 'need_user_confirm',
      created_at: '2026-08-01T07:36:42+00:00',
      payload: {
        confirmation_id: 'confirm_1',
        confirmation_type: 'section_generation_config',
        sections: [{ section_id: 's1', title: '章节一' }]
      }
    })

    const updated = store.updateMessage('conv_task_message_update', 'evt_confirm', {
      confirmed: true,
      confirming: false
    })

    expect(updated).toBe(true)
    expect(store.activeConversation.taskRunBlocks?.[0].messages[0]).toEqual(
      expect.objectContaining({
        id: 'evt_confirm',
        confirmed: true,
        confirming: false
      })
    )
  })

  it('removes messages stored inside task run blocks', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_task_message_remove',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_task_message_remove')
    store.setActiveConversation('conv_task_message_remove')
    store.applyLiveTaskEvent('conv_task_message_remove', 'task_format_loss', {
      event_id: 'evt_format_loss',
      task_id: 'task_format_loss',
      event_type: 'format_loss_confirm_requested',
      created_at: '2026-08-01T07:36:42+00:00',
      payload: {
        losses: [{ element: 'bookmark_1', message: '书签丢失' }],
        loss_count: 1
      }
    })

    const removed = store.removeMessage('conv_task_message_remove', 'evt_format_loss')

    expect(removed).toBe(true)
    expect(store.activeConversation.messages).toHaveLength(0)
    expect(store.activeConversation.taskRunBlocks?.[0].messages.some((message) => message.id === 'evt_format_loss')).toBe(false)
  })

  it('syncs live terminal status timing into latestTask and task run block', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_live_terminal',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_live_terminal')
    store.setActiveConversation('conv_live_terminal')
    store.setLatestTask('conv_live_terminal', {
      task_id: 'task_live_terminal',
      task_type: 'dynamic_agent',
      status: 'running',
      events_url: '/api/agent/tasks/task_live_terminal/events',
      startedAt: '2026-08-14T00:20:53+08:00'
    })
    store.applyLiveTaskEvent('conv_live_terminal', 'task_live_terminal', {
      event_id: 'evt_started',
      task_id: 'task_live_terminal',
      event_type: 'task_created',
      created_at: '2026-08-14T00:20:53+08:00',
      payload: {}
    })

    store.updateLatestTaskStatus('conv_live_terminal', 'completed', {
      taskId: 'task_live_terminal',
      completedAt: '2026-08-14T00:22:29+08:00'
    })

    expect(store.activeConversation.latestTask).toEqual(
      expect.objectContaining({
        status: 'completed',
        completedAt: '2026-08-14T00:22:29+08:00',
        durationMs: 96000
      })
    )
    expect(store.activeConversation.tasks?.[0]).toEqual(
      expect.objectContaining({
        status: 'completed',
        completedAt: '2026-08-14T00:22:29+08:00',
        durationMs: 96000
      })
    )
    expect(store.activeConversation.taskRunBlocks?.[0]).toEqual(
      expect.objectContaining({
        status: 'completed',
        completedAt: '2026-08-14T00:22:29+08:00',
        durationMs: 96000
      })
    )
  })

  it('syncs terminal timing to the active live task block when event task id mismatches local task id', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_live_terminal_mismatch',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_live_terminal_mismatch')
    store.setActiveConversation('conv_live_terminal_mismatch')
    store.setLatestTask('conv_live_terminal_mismatch', {
      task_id: 'task_public',
      task_type: 'dynamic_agent',
      status: 'running',
      events_url: '/api/agent/tasks/task_public/events',
      startedAt: '2026-08-14T00:51:21+08:00'
    })
    store.applyLiveTaskEvent('conv_live_terminal_mismatch', 'task_public', {
      event_id: 'evt_started',
      task_id: 'task_public',
      event_type: 'task_created',
      created_at: '2026-08-14T00:51:21+08:00',
      payload: {}
    })

    store.updateLatestTaskStatus('conv_live_terminal_mismatch', 'completed', {
      taskId: '123',
      completedAt: '2026-08-14T00:52:26+08:00'
    })

    expect(store.activeConversation.latestTask).toEqual(
      expect.objectContaining({
        task_id: 'task_public',
        status: 'completed',
        completedAt: '2026-08-14T00:52:26+08:00'
      })
    )
    expect(store.activeConversation.taskRunBlocks?.[0]).toEqual(
      expect.objectContaining({
        taskId: 'task_public',
        status: 'completed',
        completedAt: '2026-08-14T00:52:26+08:00',
        durationMs: 65000
      })
    )
  })

  it('syncs terminal status to latestTask when terminal event matches another task id shape', async () => {
    vi.mocked(conversationApi.fetchConversationDetail).mockResolvedValue({
      ...conversationDetail,
      id: 'conv_live_terminal_dual_ids',
      latestTask: null,
      tasks: [],
      messages: []
    })

    const store = useConversationStore()
    await store.fetchConversationDetail('conv_live_terminal_dual_ids')
    store.setActiveConversation('conv_live_terminal_dual_ids')
    store.setLatestTask('conv_live_terminal_dual_ids', {
      task_id: 'task_public',
      task_type: 'dynamic_agent',
      status: 'running',
      events_url: '/api/agent/tasks/task_public/events',
      startedAt: '2026-08-14T00:51:21+08:00'
    })
    store.activeConversation.tasks = [
      ...(store.activeConversation.tasks ?? []),
      {
        task_id: '123',
        task_type: 'dynamic_agent',
        status: 'running',
        events_url: '/api/agent/tasks/123/events',
        startedAt: '2026-08-14T00:51:21+08:00'
      }
    ]
    store.applyLiveTaskEvent('conv_live_terminal_dual_ids', 'task_public', {
      event_id: 'evt_started',
      task_id: 'task_public',
      event_type: 'task_created',
      created_at: '2026-08-14T00:51:21+08:00',
      payload: {}
    })

    store.updateLatestTaskStatus('conv_live_terminal_dual_ids', 'completed', {
      taskId: '123',
      completedAt: '2026-08-14T00:52:26+08:00'
    })

    expect(store.activeConversation.latestTask).toEqual(
      expect.objectContaining({
        task_id: 'task_public',
        status: 'completed',
        completedAt: '2026-08-14T00:52:26+08:00',
        durationMs: 65000
      })
    )
    expect(store.activeConversation.taskRunBlocks?.[0]).toEqual(
      expect.objectContaining({
        taskId: 'task_public',
        status: 'completed',
        completedAt: '2026-08-14T00:52:26+08:00',
        durationMs: 65000
      })
    )
  })

  it('reuses an existing empty conversation when starting a new task from another chat', async () => {
    const store = useConversationStore()
    store.conversations = [
      conversation({
        id: 'conv_busy',
        title: '打招呼',
        messages: [message('msg_hi', 'user', '你好', '2026-08-01T08:00:00+00:00')],
        messageCount: 1
      } as Partial<Conversation>),
      conversation({
        id: 'conv_empty_existing',
        title: '新会话',
        state: 'empty',
        messageCount: 0
      } as Partial<Conversation>)
    ]
    store.setActiveConversation('conv_busy')

    const target = await (store as any).startNewConversation()

    expect(target.id).toBe('conv_empty_existing')
    expect(store.activeConversationId).toBe('conv_empty_existing')
    expect(conversationApi.createConversation).not.toHaveBeenCalled()
  })

  it('keeps the current empty conversation when starting a new task from it', async () => {
    const store = useConversationStore()
    store.conversations = [
      conversation({
        id: 'conv_empty_current',
        title: '新会话',
        state: 'empty',
        messageCount: 0
      } as Partial<Conversation>)
    ]
    store.setActiveConversation('conv_empty_current')

    const target = await (store as any).startNewConversation()

    expect(target.id).toBe('conv_empty_current')
    expect(store.activeConversationId).toBe('conv_empty_current')
    expect(conversationApi.createConversation).not.toHaveBeenCalled()
  })

  it('reuses a conversation that has files but no chat messages', async () => {
    const store = useConversationStore()
    store.conversations = [
      conversation({
        id: 'conv_busy',
        title: '打招呼',
        messages: [message('msg_hi', 'user', '你好', '2026-08-01T08:00:00+00:00')],
        messageCount: 1
      } as Partial<Conversation>),
      conversation({
        id: 'conv_file_only',
        title: '新会话',
        state: 'empty',
        messageCount: 0,
        fileCount: 1
      } as Partial<Conversation>)
    ]
    store.setActiveConversation('conv_busy')

    const target = await (store as any).startNewConversation()

    expect(target.id).toBe('conv_file_only')
    expect(store.activeConversationId).toBe('conv_file_only')
    expect(conversationApi.createConversation).not.toHaveBeenCalled()
  })

  it('shares one creation request when new task is clicked repeatedly before it resolves', async () => {
    let resolveCreation: ((conversation: Conversation) => void) | undefined
    vi.mocked(conversationApi.createConversation).mockImplementation(
      () => new Promise<Conversation>((resolve) => { resolveCreation = resolve })
    )
    const store = useConversationStore()
    store.conversations = [conversation({
      id: 'conv_busy',
      messages: [message('msg_hi', 'user', '你好', '2026-08-01T08:00:00+00:00')],
      messageCount: 1
    } as Partial<Conversation>)]
    store.setActiveConversation('conv_busy')

    const first = (store as any).startNewConversation()
    const second = (store as any).startNewConversation()

    expect(conversationApi.createConversation).toHaveBeenCalledOnce()
    resolveCreation?.(conversation({ id: 'conv_created', messageCount: 0 } as Partial<Conversation>))
    await expect(first).resolves.toMatchObject({ id: 'conv_created' })
    await expect(second).resolves.toMatchObject({ id: 'conv_created' })
  })

  it('keeps knowledge mode scoped by conversation id and persists toggles', async () => {
    vi.mocked(conversationApi.fetchConversationContextSettings).mockImplementation(async (conversationId) => ({
      memory_mode: 'inherit',
      knowledge_mode: conversationId === 'conv_a' ? 'MAAS_STRICT' : 'AUTO',
      context_workspace_key: `conversation:${conversationId}`,
      context_engine_version: 'v3',
      policy_version: '2026-08-09'
    }))

    const store = useConversationStore()

    await store.loadKnowledgeMode('conv_a')
    await store.loadKnowledgeMode('conv_b')

    expect(store.getKnowledgeMode('conv_a')).toBe('MAAS_STRICT')
    expect(store.getKnowledgeMode('conv_b')).toBe('AUTO')

    await store.toggleKnowledgeMode('conv_b')

    expect(conversationApi.updateConversationKnowledgeMode).toHaveBeenCalledWith('conv_b', 'MAAS_STRICT')
    expect(store.getKnowledgeMode('conv_a')).toBe('MAAS_STRICT')
    expect(store.getKnowledgeMode('conv_b')).toBe('MAAS_STRICT')
  })

  it('rolls back knowledge mode when server persistence fails', async () => {
    vi.mocked(conversationApi.updateConversationKnowledgeMode).mockRejectedValueOnce(new Error('server down'))
    const store = useConversationStore()
    await store.loadKnowledgeMode('conv_b')

    await expect(store.setKnowledgeMode('conv_b', 'MAAS_STRICT')).rejects.toThrow('server down')

    expect(store.getKnowledgeMode('conv_b')).toBe('AUTO')
  })

  it('hands a Project first message to the chat workspace exactly once', () => {
    const store = useConversationStore()

    store.queueInitialMessage('conv_project', 'create a test plan')

    expect(store.takeInitialMessage('conv_project')).toBe('create a test plan')
    expect(store.takeInitialMessage('conv_project')).toBeNull()
  })

  it('hands Project launch files to the chat workspace exactly once', () => {
    const store = useConversationStore()
    const file = { name: 'requirements.docx', size: 128 } as File

    store.queueInitialFiles('conv_project', [file])

    expect(store.takeInitialFiles('conv_project')).toEqual([file])
    expect(store.takeInitialFiles('conv_project')).toEqual([])
  })

  it('hands a server-created template attachment to the chat workspace exactly once', () => {
    const store = useConversationStore()
    const attachment: FileAttachment = { id: 'file_template', name: 'plan.docx', size: '2 KB', extension: '.docx', type: 'test_plan_template', status: 'uploaded' }

    store.queueInitialAttachments('conv_template', [attachment])

    expect(store.takeInitialAttachments('conv_template')).toEqual([attachment])
    expect(store.takeInitialAttachments('conv_template')).toEqual([])
  })

  it('clears the cached Project identity after a conversation is detached', () => {
    const store = useConversationStore()
    const projectConversation = conversation({
      id: 'conv_project',
      projectId: 'prj_alpha',
      projectName: 'Alpha Project'
    })
    store.mergeConversationSummary(projectConversation)

    store.setConversationProject('conv_project', null)

    expect(store.conversations[0].projectId).toBeNull()
    expect(store.conversations[0].projectName).toBeNull()
  })
})
