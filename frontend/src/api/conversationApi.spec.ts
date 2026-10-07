import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  compactConversationContext,
  createConversation,
  fetchConversationContextSettings,
  fetchContextUsage,
  previewContextUsage,
  fetchConversationDetail,
  fetchConversations,
  updateConversationKnowledgeMode
} from './conversationApi'
import { clearAuthToken } from './request'

function okResponse(data: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify({ code: 0, message: 'success', data }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    })
  )
}

describe('conversationApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    clearAuthToken()
  })

  it('keeps a newly created backend conversation in the empty workspace state', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        conversation_id: 'conv_new_backend',
        title: '新会话',
        status: 'created',
        latest_task: null,
        updated_at: '2026-06-20T10:00:00+08:00'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const conversation = await createConversation()

    expect(conversation).toEqual(
      expect.objectContaining({
        id: 'conv_new_backend',
        state: 'empty',
        messages: [],
        files: []
      })
    )
  })

  it('maps server message_count for conversation summaries', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        conversations: [
          {
            conversation_id: 'conv_empty',
            title: '新会话',
            status: 'active',
            message_count: 0,
            file_count: 0,
            latest_task: null,
            updated_at: '2026-08-01T08:00:00+00:00'
          },
          {
            conversation_id: 'conv_busy',
            title: '打招呼',
            status: 'active',
            message_count: 2,
            file_count: 1,
            latest_task: null,
            updated_at: '2026-08-01T08:05:00+00:00'
          }
        ],
        total: 2
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const conversations = await fetchConversations()

    expect(conversations.map((item) => [item.id, item.messageCount, item.fileCount])).toEqual([
      ['conv_empty', 0, 0],
      ['conv_busy', 2, 1]
    ])
  })

  it('maps Project identity for sidebar conversation labels', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse({
      conversations: [{
        conversation_id: 'conv_project',
        title: 'Project chat',
        status: 'active',
        project_id: 'prj_alpha',
        project_name: 'A very long project name',
        updated_at: '2026-09-09T09:00:00+08:00'
      }],
      total: 1
    })))

    const [conversation] = await fetchConversations()

    expect(conversation).toEqual(expect.objectContaining({
      projectId: 'prj_alpha',
      projectName: 'A very long project name'
    }))
  })

  it('maps conversation detail tasks without replacing them with latestTask summary', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        conversation: {
          conversation_id: 'conv_efd5c72e',
          title: '生成电商订单与售后服务平台测试方案',
          status: 'completed',
          latest_task: {
            task_id: 'task_7e15d3a4',
            status: 'completed',
            events_url: '/api/agent/tasks/task_7e15d3a4/events'
          }
        },
        latest_task: {
          task_id: 'task_7e15d3a4',
          status: 'completed',
          events_url: '/api/agent/tasks/task_7e15d3a4/events'
        },
        tasks: [
          {
            task_id: 'task_7e15d3a4',
            task_type: 'test_plan_generation',
            status: 'completed',
            events_url: '/api/agent/tasks/task_7e15d3a4/events',
            trigger_message_id: 'msg_3129a663',
            started_at: '2026-08-01T04:58:45+00:00',
            completed_at: '2026-08-01T05:01:34+00:00',
            duration_ms: 169000
          }
        ]
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const conversation = await fetchConversationDetail('conv_efd5c72e')

    expect(conversation.latestTask).toEqual(
      expect.objectContaining({
        task_id: 'task_7e15d3a4',
        triggerMessageId: null,
        startedAt: undefined,
        completedAt: undefined,
        durationMs: undefined
      })
    )
    expect(conversation.tasks).toEqual([
      expect.objectContaining({
        task_id: 'task_7e15d3a4',
        triggerMessageId: 'msg_3129a663',
        startedAt: '2026-08-01T04:58:45+00:00',
        completedAt: '2026-08-01T05:01:34+00:00',
        durationMs: 169000
      })
    ])
  })

  it('falls back to latestTask only when detail omits tasks', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        conversation: {
          conversation_id: 'conv_legacy',
          title: 'legacy',
          status: 'planning'
        },
        latest_task: {
          task_id: 'task_legacy',
          task_type: 'test_plan_generation',
          status: 'running',
          trigger_message_id: 'msg_legacy',
          started_at: '2026-08-01T04:58:45+00:00'
        }
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const conversation = await fetchConversationDetail('conv_legacy')

    expect(conversation.tasks).toEqual([
      expect.objectContaining({
        task_id: 'task_legacy',
        triggerMessageId: 'msg_legacy',
        startedAt: '2026-08-01T04:58:45+00:00'
      })
    ])
  })

  it('fetches context usage from the frozen conversation public-id endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        conversation_public_id: 'conv_bb4e5478',
        model: {
          name: 'qwen3.7-max',
          context_window_tokens: 128000,
          window_source: 'model_config'
        },
        usage: {
          used_tokens: 86000,
          available_tokens: 42000,
          percent: 67.2,
          count_mode: 'exact',
          estimated: false,
          over_limit: false
        },
        breakdown: {
          conversation_history: 61000,
          project_documents: 12000,
          task_context: 7000,
          user_memory: 4000,
          system_instructions: 2000
        },
        compaction: {
          available: true,
          recommended: false,
          in_progress: false
        },
        as_of: '2026-08-08T12:00:00Z'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const controller = new AbortController()
    const result = await fetchContextUsage('conv_bb4e5478', { signal: controller.signal })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv_bb4e5478/context-usage',
      expect.objectContaining({
        method: 'GET',
        signal: controller.signal
      })
    )
    expect(result.model.name).toBe('qwen3.7-max')
  })

  it('compacts context through the frozen conversation public-id endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        run_public_id: 'ctxcmp_123',
        summary_public_id: 'ctxsum_456',
        before_used_tokens: 86000,
        after_used_tokens: 32000,
        saved_tokens: 54000,
        summary_updated: true,
        as_of: '2026-08-08T12:05:00Z',
        in_progress: false
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await compactConversationContext('conv_bb4e5478')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv_bb4e5478/context/compact',
      expect.objectContaining({
        method: 'POST'
      })
    )
    expect(result.saved_tokens).toBe(54000)
  })

  it('previews the next chat context without sending a message', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        conversation_public_id: 'conv_bb4e5478',
        model: { name: 'qwen3.7-max', context_window_tokens: 200000, window_source: 'model_config' },
        usage: { used_tokens: 3210, available_tokens: 196790, percent: 1.61, count_mode: 'heuristic', estimated: true, over_limit: false },
        breakdown: { conversation_history: 2800, project_documents: 0, task_context: 250, user_memory: 0, system_instructions: 0 },
        compaction: { available: true, recommended: false, in_progress: false },
        available: true,
        snapshot_public_id: null,
        source: 'next_request_preview',
        as_of: '2026-09-26T00:00:00Z'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    await previewContextUsage('conv_bb4e5478', {
      content: '请继续分析退款项目的上线风险',
      attached_file_ids: ['file_1']
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv_bb4e5478/context-usage/preview',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          content: '请继续分析退款项目的上线风险',
          attached_file_ids: ['file_1']
        })
      })
    )
  })

  it('fetches conversation-scoped knowledge mode from context settings', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        memory_mode: 'inherit',
        knowledge_mode: 'MAAS_STRICT',
        context_workspace_key: 'conversation:conv_kb',
        context_engine_version: 'v3',
        policy_version: '2026-08-09'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const settings = await fetchConversationContextSettings('conv_kb')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv_kb/context-settings',
      expect.objectContaining({ method: 'GET' })
    )
    expect(settings.knowledge_mode).toBe('MAAS_STRICT')
  })

  it('updates conversation-scoped knowledge mode through context settings', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        knowledge_mode: 'AUTO'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const settings = await updateConversationKnowledgeMode('conv_kb', 'AUTO')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv_kb/context-settings',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ knowledge_mode: 'AUTO' })
      })
    )
    expect(settings.knowledge_mode).toBe('AUTO')
  })
})
