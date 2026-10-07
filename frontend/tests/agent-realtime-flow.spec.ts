import { describe, expect, it, vi } from 'vitest'
import { createTaskEventHandler } from '../src/composables/useTaskEvents'
import type { ChatMessage } from '../src/types'
import chatMessageListSource from '../src/components/chat/ChatMessageList.vue?raw'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'
import sectionConfirmSource from '../src/components/cards/SectionConfirmCard.vue?raw'

describe('agent realtime task flow', () => {
  it('maps plan_created into an execution plan message', () => {
    const appended: ChatMessage[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: (message) => appended.push(message),
      onUpdateMessage: vi.fn()
    })

    handler('plan_created', {
      event_id: 'evt_plan',
      task_id: 'task_001',
      payload: {
        steps: [
          { step_id: 'parse_requirement', name: '解析需求文档', status: 'running' },
          { step_id: 'generate_plan', name: '生成测试方案', status: 'pending' }
        ]
      }
    })

    expect(appended[0]).toEqual(
      expect.objectContaining({
        id: 'evt_plan',
        type: 'agent_plan',
        taskId: 'task_001',
        plan: [
          expect.objectContaining({ id: 'parse_requirement', title: '解析需求文档', status: 'running' }),
          expect.objectContaining({ id: 'generate_plan', title: '生成测试方案', status: 'pending' })
        ]
      })
    )
  })

  it('updates a running tool call with success and failure details from real SSE fields', () => {
    const appended: ChatMessage[] = []
    const updates: Array<{ id: string; patch: Partial<ChatMessage> }> = []
    const handler = createTaskEventHandler({
      onAppendMessage: (message) => appended.push(message),
      onUpdateMessage: (id, patch) => {
        updates.push({ id, patch })
        return true
      }
    })

    handler('tool_started', {
      event_id: 'evt_tool_start',
      task_id: 'task_001',
      payload: {
        tool_call_id: 'tc_req',
        tool_name: 'RequirementParserTool',
        input: { file_id: 'file_req' },
        text: '开始解析需求文档'
      }
    })
    handler('tool_finished', {
      event_id: 'evt_tool_done',
      task_id: 'task_001',
      payload: {
        tool_call_id: 'tc_req',
        tool_name: 'RequirementParserTool',
        output: { modules: 4 },
        duration_ms: 1234,
        text: '需求文档解析完成'
      }
    })
    handler('tool_failed', {
      event_id: 'evt_tool_fail',
      task_id: 'task_001',
      payload: {
        tool_call_id: 'tc_kb',
        tool_name: 'KnowledgeSearchTool',
        input: '支付系统',
        error: '知识库连接失败',
        duration_ms: 500
      }
    })

    expect(appended[0].toolCall).toEqual(
      expect.objectContaining({
        id: 'tc_req',
        name: 'RequirementParserTool',
        status: 'running',
        input: '{\n  "file_id": "file_req"\n}',
        output: '开始解析需求文档',
        startedAt: expect.any(String)
      })
    )
    expect(updates[0]).toEqual(
      expect.objectContaining({
        id: 'tool_tc_req',
        patch: expect.objectContaining({
          toolCall: expect.objectContaining({
            status: 'success',
            output: '{\n  "modules": 4\n}',
            duration: '3s',
            startedAt: expect.any(String),
            finishedAt: expect.any(String)
          })
        })
      })
    )
    expect(updates[1].patch.toolCall).toEqual(
      expect.objectContaining({
        id: 'tc_kb',
        status: 'failed',
        input: '支付系统',
        output: '知识库连接失败',
        duration: '3s'
      })
    )
  })

  it('maps need_user_confirm into a section confirmation message with task metadata', () => {
    const appended: ChatMessage[] = []
    const handler = createTaskEventHandler({
      onAppendMessage: (message) => appended.push(message),
      onUpdateMessage: vi.fn()
    })

    handler('need_user_confirm', {
      event_id: 'evt_confirm',
      task_id: 'task_001',
      payload: {
        confirmation_id: 'confirm_001',
        confirmation_type: 'section_generation_config',
        sections: [
          {
            section_id: 'sec_001',
            section_number: '1',
            section_title: '项目概述',
            level: 'H1',
            suggested_action: 'ai_generate',
            reason: '需要根据需求生成'
          }
        ]
      }
    })

    expect(appended[0]).toEqual(
      expect.objectContaining({
        id: 'evt_confirm',
        type: 'section_confirm',
        taskId: 'task_001',
        confirmationId: 'confirm_001',
        confirmationType: 'section_generation_config',
        sections: [
          expect.objectContaining({
            id: 'sec_001',
            code: '1',
            title: '项目概述',
            suggestedAction: 'ai_generate',
            action: 'ai_generate'
          })
        ]
      })
    )
  })

  it('wires section confirmation submit back to the workspace SSE loop', () => {
    expect(sectionConfirmSource).toContain("confirm: [sections: SectionItem[]]")
    expect(chatMessageListSource).toContain('@confirm=')
    expect(chatMessageListSource).toContain("'confirm-section'")
    expect(chatWorkspaceSource).toContain('async function handleConfirmSections')
    expect(chatWorkspaceSource).toContain('agentApi.confirmTask')
    expect(chatWorkspaceSource).toContain('connectTaskEvents(activeConversation.id, confirmedTask.events_url, confirmedTask.task_id)')
  })

  it('enables the chapter confirmation action after a valid confirmation card arrives', () => {
    expect(chatWorkspaceSource).toContain('conversationStore.updateLatestTaskStatus(conversationId, status, {')
    expect(chatWorkspaceSource).toContain("status === 'waiting_user_confirm'")
    expect(chatWorkspaceSource).toContain("message.type === 'section_confirm'")
    expect(chatWorkspaceSource).toContain("!message.confirmed")
  })
})
