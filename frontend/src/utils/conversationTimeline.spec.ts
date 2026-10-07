import { describe, expect, it } from 'vitest'
import { assembleConversationTimeline } from './conversationTimeline'
import type { AgentTask, ChatMessage, TaskRunBlock } from '@/types'

const triggerMessageId = 'msg_3129a663'
const taskId = 'task_7e15d3a4'

function userMessage(id: string, sequence: number, text: string): ChatMessage {
  return {
    id,
    type: 'user_text',
    role: 'user',
    text,
    conversationSequence: sequence,
    createdAt: `2026-08-01T05:00:0${sequence}+00:00`,
    timestamp: ''
  }
}

function agentMessage(id: string, sequence: number, text: string): ChatMessage {
  return {
    id,
    type: 'agent_text',
    role: 'agent',
    text,
    conversationSequence: sequence,
    createdAt: `2026-08-01T05:00:0${sequence}+00:00`,
    timestamp: ''
  }
}

function taskBlock(overrides: Partial<TaskRunBlock> = {}): TaskRunBlock {
  return {
    taskId,
    taskType: 'test_plan_generation',
    triggerMessageId,
    status: 'completed',
    startedAt: '2026-08-01T04:58:45+00:00',
    completedAt: '2026-08-01T05:01:34+00:00',
    durationMs: 169000,
    lastUpdateMode: 'hydrate',
    collapsed: true,
    userCollapseOverride: null,
    events: [],
    seenEventIds: {},
    messages: [],
    toolExecutions: [],
    dynamicNarratives: [],
    toolNarratives: [],
    artifacts: [],
    ...overrides
  }
}

describe('conversation timeline task anchoring', () => {
  it('keeps a pending thinking message adjacent to its server-sequenced user message', () => {
    const items = assembleConversationTimeline([
      userMessage('msg_previous', 1, '上一条消息'),
      {
        ...userMessage('msg_server_user', 2, '当前用户消息'),
        clientOrder: 8,
        timelineRank: 0
      },
      agentMessage('msg_after', 3, '后续历史消息'),
      {
        id: 'msg_thinking_pending',
        type: 'agent_text',
        role: 'agent',
        text: '正在思考...',
        thinking: true,
        clientOrder: 8,
        timelineRank: 1,
        anchorMessageId: 'msg_server_user',
        timestamp: ''
      }
    ], [], [])

    expect(items.map((item) => item.id)).toEqual([
      'msg_previous',
      'msg_server_user',
      'msg_thinking_pending',
      'msg_after'
    ])
  })

  it('keeps agent-shaped messages on the assistant side after a stream race', () => {
    const items = assembleConversationTimeline([
      {
        id: 'msg_agent_race',
        type: 'agent_text',
        // This is the corrupted intermediate state seen when a server
        // payload overwrites the optimistic message envelope.
        role: 'user',
        text: '正在思考...',
        thinking: true,
        timestamp: ''
      }
    ], [], [])

    expect(items[0]).toMatchObject({
      id: 'msg_agent_race',
      kind: 'agent'
    })
  })

  it('anchors a task block with block.triggerMessageId when latestTask summary has no trigger', () => {
    const items = assembleConversationTimeline(
      [
        userMessage(triggerMessageId, 1, '帮我生成测试方案'),
        userMessage('msg_weather', 2, '今天天气怎么样'),
        agentMessage('msg_weather_reply', 3, '无法提供实时天气。'),
        userMessage('msg_hello', 4, '你好'),
        agentMessage('msg_hello_reply', 5, '你好！')
      ],
      [taskBlock()],
      [
        {
          task_id: taskId,
          task_type: 'test_plan_generation',
          status: 'completed',
          events_url: `/api/agent/tasks/${taskId}/events`,
          triggerMessageId: null
        } as AgentTask
      ]
    )

    expect(items.map((item) => item.id)).toEqual([
      triggerMessageId,
      `run_${taskId}`,
      'msg_weather',
      'msg_weather_reply',
      'msg_hello',
      'msg_hello_reply'
    ])
  })

  it('keeps the task block anchored after later ordinary messages are added', () => {
    const laterMessages = Array.from({ length: 5 }, (_, index) =>
      userMessage(`msg_later_${index}`, index + 6, `后续消息 ${index}`)
    )

    const items = assembleConversationTimeline(
      [
        userMessage(triggerMessageId, 1, '帮我生成测试方案'),
        userMessage('msg_weather', 2, '今天天气怎么样'),
        agentMessage('msg_weather_reply', 3, '无法提供实时天气。'),
        ...laterMessages
      ],
      [taskBlock({ status: 'completed' })],
      []
    )

    expect(items[0].id).toBe(triggerMessageId)
    expect(items[1].id).toBe(`run_${taskId}`)
    expect(items.at(-1)?.id).toBe('msg_later_4')
  })

  it('uses the legacy end fallback only when neither block nor task has a trigger', () => {
    const items = assembleConversationTimeline(
      [
        userMessage(triggerMessageId, 1, '帮我生成测试方案'),
        userMessage('msg_weather', 2, '今天天气怎么样')
      ],
      [taskBlock({ triggerMessageId: null })],
      []
    )

    expect(items.map((item) => item.id)).toEqual([
      triggerMessageId,
      'msg_weather',
      `run_${taskId}`
    ])
  })
})
