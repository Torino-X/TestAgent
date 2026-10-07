import { afterEach, describe, expect, it, vi } from 'vitest'
import { confirmFileType, fetchConversationFiles, uploadFile } from './fileApi'
import { fetchMessages, sendMessage } from './messageApi'
import { fetchKnowledgeBaseSettings, fetchModelSettings, saveAllSettings } from './settingsApi'
import { clearAuthToken } from './request'
import {
  cancelTask,
  confirmTask,
  fetchPendingConfirmation,
  fetchTaskEvents,
  retryTask
} from './agentApi'
import { downloadArtifact, fetchTaskArtifacts } from './artifactApi'

function okResponse(data: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify({ code: 0, message: 'success', data }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    })
  )
}

describe('workflow API integration', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    clearAuthToken()
  })

  it('uploads files as multipart form data with conversation_id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        file_id: 'file_001',
        file_name: '需求文档.docx',
        file_size: 2048,
        file_ext: '.docx',
        file_type: 'requirement_doc',
        status: 'uploaded'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const file = Object.assign(new Blob(['hello']), { name: '需求文档.docx' }) as File
    const uploaded = await uploadFile(file, 'conv_001', 'requirement_doc')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/files/upload',
      expect.objectContaining({
        method: 'POST',
        body: expect.any(FormData)
      })
    )
    const formData = fetchMock.mock.calls[0][1].body as FormData
    expect(formData.get('conversation_id')).toBe('conv_001')
    expect(formData.get('file_type')).toBe('requirement_doc')
    expect(uploaded).toEqual(
      expect.objectContaining({
        id: 'file_001',
        name: '需求文档.docx',
        type: 'requirement_doc'
      })
    )
  })

  it('confirms manually selected file types through the documented endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        file_id: 'file_001',
        file_name: 'template.md',
        file_size: 4096,
        file_ext: '.md',
        file_type: 'test_plan_template',
        status: 'confirmed'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const updated = await confirmFileType('file_001', 'test_plan_template')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/files/file_001/confirm-type',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ file_type: 'test_plan_template' })
      })
    )
    expect(updated).toEqual(
      expect.objectContaining({
        id: 'file_001',
        type: 'test_plan_template',
        status: 'confirmed'
      })
    )
  })

  it('maps documented conversation file list envelopes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        items: [
          {
            file_id: 'file_req_001',
            original_name: 'requirement.docx',
            file_size: 204800,
            file_ext: '.docx',
            file_type: 'requirement_doc',
            upload_status: 'uploaded'
          }
        ]
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const files = await fetchConversationFiles('conv_001')

    expect(fetchMock).toHaveBeenCalledWith('/api/conversations/conv_001/files', expect.any(Object))
    expect(files[0]).toEqual(
      expect.objectContaining({
        id: 'file_req_001',
        name: 'requirement.docx',
        status: 'uploaded'
      })
    )
  })

  it('maps documented conversation message list envelopes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        items: [
          {
            message_id: 'msg_001',
            role: 'user',
            message_type: 'user_text',
            content: 'hello',
            created_at: '2026-06-19T21:30:00+08:00'
          }
        ]
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const messages = await fetchMessages('conv_001')

    expect(fetchMock).toHaveBeenCalledWith('/api/conversations/conv_001/messages', expect.any(Object))
    expect(messages[0]).toEqual(
      expect.objectContaining({
        id: 'msg_001',
        text: 'hello'
      })
    )
  })

  it('orders historical messages by backend creation time', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        items: [
          {
            message_id: 'msg_agent_later',
            role: 'agent',
            message_type: 'agent_text',
            content: 'agent reply',
            created_at: '2026-06-19T21:32:00+08:00'
          },
          {
            message_id: 'msg_user_first',
            role: 'user',
            message_type: 'user_text',
            content: 'first prompt',
            created_at: '2026-06-19T21:30:00+08:00'
          },
          {
            message_id: 'msg_user_second',
            role: 'user',
            message_type: 'user_text',
            content: 'second prompt',
            created_at: '2026-06-19T21:31:00+08:00'
          }
        ]
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const messages = await fetchMessages('conv_001')

    expect(messages.map((message) => message.id)).toEqual([
      'msg_user_first',
      'msg_user_second',
      'msg_agent_later'
    ])
  })

  it('keeps a user message before its agent reply when their historical timestamps match', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        items: [
          {
            message_id: 'msg_agent_reply',
            role: 'agent',
            message_type: 'agent_text',
            content: 'assistant reply',
            timestamp: '2026-07-14T12:00:00+08:00'
          },
          {
            message_id: 'msg_user_prompt',
            role: 'user',
            message_type: 'user_text',
            content: 'user prompt',
            timestamp: '2026-07-14T12:00:00+08:00'
          }
        ]
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const messages = await fetchMessages('conv_001')

    expect(messages.map((message) => message.id)).toEqual([
      'msg_user_prompt',
      'msg_agent_reply'
    ])
  })

  it('drops blank historical agent text placeholders', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        items: [
          {
            message_id: 'msg_user_task',
            role: 'user',
            message_type: 'user_text',
            content: 'generate test plan',
            created_at: '2026-06-19T21:30:00+08:00'
          },
          {
            message_id: 'msg_blank_agent',
            role: 'agent',
            message_type: 'agent_text',
            content: '',
            created_at: '2026-06-19T21:31:00+08:00'
          },
          {
            message_id: 'msg_user_followup',
            role: 'user',
            message_type: 'user_text',
            content: 'analyze this error log',
            created_at: '2026-06-19T21:32:00+08:00'
          }
        ]
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const messages = await fetchMessages('conv_001')

    expect(messages.map((message) => message.id)).toEqual(['msg_user_task', 'msg_user_followup'])
  })

  it('restores files attached to a historical user message', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        items: [
          {
            message_id: 'msg_with_files',
            role: 'user',
            message_type: 'user_text',
            content: '根据这些文件生成测试方案',
            attached_files: [
              {
                file_id: 'file_req_001',
                original_name: '需求文档.docx',
                file_size: 204800,
                file_ext: '.docx',
                file_type: 'requirement_doc',
                upload_status: 'uploaded'
              }
            ],
            created_at: '2026-06-19T21:30:00+08:00'
          }
        ]
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const messages = await fetchMessages('conv_001')

    expect(messages[0].files).toEqual([
      expect.objectContaining({
        id: 'file_req_001',
        name: '需求文档.docx',
        type: 'requirement_doc',
        status: 'uploaded'
      })
    ])
  })

  it('sends chat messages using documented message payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        message: {
          message_id: 'msg_001',
          role: 'user',
          message_type: 'user_text',
          content: '生成测试方案',
          created_at: '2026-06-19T21:30:00+08:00'
        },
        agent_task: {
          task_id: 'task_001',
          task_type: 'test_plan_generation',
          status: 'planning',
          events_url: '/api/agent/tasks/task_001/events'
        }
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await sendMessage('conv_001', '生成测试方案', ['file_001'])

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv_001/messages',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          content: '生成测试方案',
          attached_file_ids: ['file_001']
        })
      })
    )
    expect(result.message).toEqual(expect.objectContaining({ id: 'msg_001', text: '生成测试方案' }))
    expect(result.agent_task?.task_id).toBe('task_001')
  })

  it('maps generated conversation title from send message response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        message: {
          message_id: 'msg_001',
          role: 'user',
          message_type: 'user_text',
          content: '帮我根据 PRD 生成测试方案',
          created_at: '2026-06-19T21:30:00+08:00'
        },
        agent_task: null,
        agent_reply: {
          message_type: 'agent_text',
          content: 'ok'
        },
        conversation: {
          id: 'conv_001',
          title: '测试方案生成',
          state: 'active',
          updated_at: '2026-06-19T21:30:01+08:00'
        }
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await sendMessage('conv_001', '帮我根据 PRD 生成测试方案')

    expect(result.conversation).toEqual(
      expect.objectContaining({
        id: 'conv_001',
        title: '测试方案生成',
        updatedAt: '2026-06-19T21:30:01+08:00'
      })
    )
  })

  it('submits section confirmation using the documented agent payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        task_id: 'task_001',
        confirmation_id: 'confirm_001',
        status: 'confirmed',
        task_status: 'running'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const task = await confirmTask(
      'task_001',
      [
        {
          id: 'sec_001',
          code: '1',
          title: '项目概述',
          level: 'H1',
          suggestedAction: 'ai_generate',
          action: 'keep_template',
          reason: '保留模板'
        }
      ],
      {
        confirmationId: 'confirm_001',
        confirmationType: 'section_generation_config'
      }
    )

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/agent/tasks/task_001/confirm',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          confirmation_type: 'section_generation_config',
          confirmation_id: 'confirm_001',
          sections: [
              {
                section_id: 'sec_001',
                section_title: '项目概述',
                action: 'keep_template'
              }
            ]
        })
      })
    )
    expect(task).toEqual(
      expect.objectContaining({
        task_id: 'task_001',
        status: 'running',
        events_url: '/api/agent/tasks/task_001/events'
      })
    )
  })

  it('downloads artifact via authenticated Blob request (Phase 2.9A.25)', async () => {
    // Phase 2.9A.25: Replaced iframe with fetch-based Blob download.
    // The download now uses the project's request infrastructure
    // (Bearer token + credentials: include) for proper authentication.
    const blobContent = new Uint8Array([0x50, 0x4B, 0x03, 0x04]) // PK zip header
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(blobContent, {
        status: 200,
        headers: {
          'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          'Content-Disposition': "attachment; filename*=UTF-8''test-report.docx"
        }
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    // Stub document.createElement for <a download> trigger
    const createdElements: Array<{ tag: string; href: string; download: string; click: () => void; remove: () => void }> = []
    const docStub = {
      createElement: (tag: string) => {
        const el = { tag, href: '', download: '', display: '', style: {} as Record<string, string>,
          click: vi.fn(), remove: vi.fn() }
        createdElements.push(el as any)
        return el
      },
      body: { appendChild: (node: unknown) => node }
    }
    vi.stubGlobal('document', docStub as unknown as Document)
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:mock-url'),
      revokeObjectURL: vi.fn()
    })

    try {
      await downloadArtifact('art_36902d99')

      // 1. Must use fetch (not iframe) with correct URL
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(fetchMock.mock.calls[0][0]).toBe('/api/artifacts/art_36902d99/download')
      // 2. Must create <a> element to trigger download
      const links = createdElements.filter((e) => e.tag === 'a')
      expect(links.length).toBe(1)
      expect(links[0].download).toBe('test-report.docx')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('rejects download when artifact ID is empty (fail-closed)', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    try {
      await expect(downloadArtifact('')).rejects.toThrow('产物信息不完整')
      await expect(downloadArtifact('  ')).rejects.toThrow('产物信息不完整')
      // Must NOT have called fetch at all
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('cancels and retries tasks through documented task control endpoints', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ task_id: 'task_001', status: 'cancelled' }))
      .mockResolvedValueOnce(
        okResponse({
          task_id: 'task_001',
          task_type: 'test_plan_generation',
          status: 'running',
          events_url: '/api/agent/tasks/task_001/events'
        })
      )
    vi.stubGlobal('fetch', fetchMock)

    const cancelled = await cancelTask('task_001', '用户取消')
    const retried = await retryTask('task_001', 'from_failed_step')

    expect(fetchMock.mock.calls[0]).toEqual([
      '/api/agent/tasks/task_001/cancel',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ reason: '用户取消' })
      })
    ])
    expect(fetchMock.mock.calls[1]).toEqual([
      '/api/agent/tasks/task_001/retry',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ retry_mode: 'from_failed_step' })
      })
    ])
    expect(cancelled.status).toBe('cancelled')
    expect(retried.events_url).toBe('/api/agent/tasks/task_001/events')
  })

  it('loads event-list, pending confirmation and artifacts for history restore', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        okResponse({
          events: [
            {
              event_id: 'evt_plan',
              task_id: 'task_001',
              event_type: 'plan_created',
              payload: { steps: [{ step_id: 'step_1', name: '解析需求', status: 'done' }] }
            }
          ]
        })
      )
      .mockResolvedValueOnce(
        okResponse({
          confirmation_id: 'confirm_001',
          confirmation_type: 'section_generation_config',
          status: 'pending',
          request: {
            sections: [
              {
                section_id: 'sec_001',
                section_number: '1',
                section_title: '项目概述',
                level: 'H1',
                suggested_action: 'ai_generate'
              }
            ]
          }
        })
      )
      .mockResolvedValueOnce(
        okResponse({
          artifacts: [
            {
              artifact_id: 'artifact_001',
              artifact_type: 'test_plan_word',
              file_name: 'report.docx',
              file_size: 2457600,
              download_url: '/api/artifacts/artifact_001/download',
              created_at: '2026-06-18T10:30:00+08:00'
            }
          ]
        })
      )
    vi.stubGlobal('fetch', fetchMock)

    const events = await fetchTaskEvents('task_001')
    const pending = await fetchPendingConfirmation('task_001')
    const artifacts = await fetchTaskArtifacts('task_001')

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/agent/tasks/task_001/event-list',
      '/api/agent/tasks/task_001/pending-confirmation',
      '/api/agent/tasks/task_001/artifacts'
    ])
    expect(events[0]).toEqual(expect.objectContaining({ event_type: 'plan_created' }))
    expect(pending).toEqual(
      expect.objectContaining({
        confirmationId: 'confirm_001',
        sections: [expect.objectContaining({ id: 'sec_001', title: '项目概述' })]
      })
    )
    expect(artifacts[0]).toEqual(
      expect.objectContaining({
        id: 'artifact_001',
        name: 'report.docx',
        downloadUrl: '/api/artifacts/artifact_001/download'
      })
    )
  })

  it('saves settings to model, knowledge-base and upload endpoints', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ config_id: 'config_model', saved: true }))
      .mockResolvedValueOnce(okResponse({ config_id: 'config_kb', saved: true }))
      .mockResolvedValueOnce(okResponse({ saved: true }))
    vi.stubGlobal('fetch', fetchMock)

    await saveAllSettings({
      apiBaseUrl: 'https://api.example.com/v1',
      apiKey: 'sk-test',
      modelName: 'qwen-plus',
      timeoutSeconds: 120,
      knowledgeBaseUrl: 'https://kb.example.com/api',
      knowledgeCollection: 'kb_001',
      enableKnowledgeBase: true,
      maxFileSizeMb: 50,
      maxFilesPerConversation: 10,
      allowedExtensions: ['.docx', '.md']
    })

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/settings/model',
      '/api/settings/knowledge-base',
      '/api/settings/upload'
    ])
  })

  it('fetches model settings from the backend response instead of mock defaults', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        api_base_url: 'https://api.minimaxi.com/v1',
        api_key_masked: 'sk-****abcd',
        model_name: 'MiniMax-M2.7',
        timeout_seconds: 120
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const settings = await fetchModelSettings()

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/settings/model',
      expect.objectContaining({ method: 'GET' })
    )
    expect(settings.apiBaseUrl).toBe('https://api.minimaxi.com/v1')
    expect(settings.apiKey).toBe('sk-****abcd')
    expect(settings.modelName).toBe('MiniMax-M2.7')
    expect(settings.timeoutSeconds).toBe(120)
  })

  it('does not add model defaults when fetching knowledge-base settings', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        api_base_url: 'https://kb.example.com/api',
        api_key_masked: '',
        knowledge_base_id: 'kb_real',
        enabled: false
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const settings = await fetchKnowledgeBaseSettings()

    expect(settings).toEqual({
      knowledgeBaseUrl: 'https://kb.example.com/api',
      knowledgeCollection: 'kb_real',
      enableKnowledgeBase: false
    })
  })

  it('does not send masked API keys back when saving settings', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ config_id: 'config_model', saved: true }))
      .mockResolvedValueOnce(okResponse({ config_id: 'config_kb', saved: true }))
      .mockResolvedValueOnce(okResponse({ saved: true }))
    vi.stubGlobal('fetch', fetchMock)

    await saveAllSettings({
      apiBaseUrl: 'https://api.example.com/v1',
      apiKey: 'sk-****abcd',
      modelName: 'qwen-plus',
      timeoutSeconds: 120,
      knowledgeBaseUrl: 'https://kb.example.com/api',
      knowledgeCollection: 'kb_001',
      enableKnowledgeBase: true,
      maxFileSizeMb: 50,
      maxFilesPerConversation: 10,
      allowedExtensions: ['.docx', '.md']
    })

    const modelBody = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    const knowledgeBody = JSON.parse(fetchMock.mock.calls[1][1].body as string)
    expect(modelBody).not.toHaveProperty('api_key')
    expect(knowledgeBody).not.toHaveProperty('api_key')
  })
})
