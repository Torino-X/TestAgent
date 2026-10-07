import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '@/types'
import { taskBlocksInput } from './taskState'
import { isTaskCompletedMessage, taskDurationLabel, taskEventTimestamps } from './taskState'
import { deriveTaskRunState } from './taskState'

describe('taskState', () => {
  it('blocks sending while the current task is not finished', () => {
    expect(taskBlocksInput({ state: 'empty', latestTask: null })).toBe(false)
    expect(taskBlocksInput({ state: 'planning', latestTask: null })).toBe(false)
    expect(
      taskBlocksInput({
        state: 'planning',
        latestTask: {
          task_id: 'task_1',
          task_type: 'test_plan_generation',
          status: 'running',
          events_url: '/events/task_1'
        }
      })
    ).toBe(true)
    expect(
      taskBlocksInput({
        state: 'completed',
        latestTask: {
          task_id: 'task_1',
          task_type: 'test_plan_generation',
          status: 'completed',
          events_url: '/events/task_1'
        }
      })
    ).toBe(false)
  })

  it('does not treat a narrative agent message as task completion', () => {
    const message = {
      id: 'message_1',
      type: 'agent_text',
      role: 'agent',
      eventType: 'agent_text',
      text: '任务已完成阶段总结，但任务仍在执行中。',
      timestamp: '2026-07-23T00:00:00.000Z'
    } as unknown as ChatMessage

    expect(isTaskCompletedMessage(message)).toBe(false)
  })

  it('only treats the explicit task_completed event as terminal', () => {
    const message = {
      id: 'message_2',
      type: 'agent_text',
      role: 'agent',
      eventType: 'task_completed',
      text: '任务完成。',
      timestamp: '2026-07-23T00:00:00.000Z'
    } as ChatMessage

    expect(isTaskCompletedMessage(message)).toBe(true)
  })

  it('uses the processed-duration label while a task is active', () => {
    expect(taskDurationLabel('running')).toBe('已处理')
    expect(taskDurationLabel('completed')).toBe('已处理')
  })
  it('uses task lifecycle event timestamps instead of unrelated conversation messages', () => {
    const timestamps = taskEventTimestamps([
      { eventType: undefined, createdAt: '2026-07-23T00:00:00.000Z' },
      { eventType: 'task_created', taskId: 'task_1', createdAt: '2026-07-23T01:00:00.000Z' },
      { eventType: 'tool_started', taskId: 'task_1', createdAt: '2026-07-23T01:00:05.000Z' },
      { eventType: undefined, taskId: 'task_1', createdAt: '2026-07-23T01:00:06.000Z' }
    ])

    expect(timestamps).toEqual([
      Date.parse('2026-07-23T01:00:00.000Z'),
      Date.parse('2026-07-23T01:00:05.000Z')
    ])
  })
})

describe('deriveTaskRunState (Phase 2.9B.5 — 任务/Tool 状态隔离)', () => {
  it('completed task + 历史 failed Tool → completed', () => {
    expect(deriveTaskRunState({ taskStatus: 'completed' })).toBe('completed')
    // 即使存在 task_failed 消息,权威 taskStatus=completed 优先。
    expect(deriveTaskRunState({ taskStatus: 'completed', explicitTaskFailed: true })).toBe('completed')
  })

  it('running task + 可降级 failed Tool → running', () => {
    expect(deriveTaskRunState({ taskStatus: 'running' })).toBe('running')
    // 局部 tool_failed 不通过任务状态输入反映。
    expect(deriveTaskRunState({ taskStatus: 'running' })).toBe('running')
  })

  it('waiting task + failed Tool → waiting', () => {
    expect(deriveTaskRunState({ taskStatus: 'waiting_user_confirm' })).toBe('waiting')
    expect(deriveTaskRunState({ taskStatus: 'running', waitingConfirmation: true })).toBe('waiting')
  })

  it('failed task → failed', () => {
    expect(deriveTaskRunState({ taskStatus: 'failed' })).toBe('failed')
    expect(deriveTaskRunState({ taskStatus: 'running', explicitTaskFailed: true })).toBe('failed')
  })

  it('cancelled task → cancelled', () => {
    expect(deriveTaskRunState({ taskStatus: 'cancelled' })).toBe('cancelled')
    expect(deriveTaskRunState({ taskStatus: 'running', explicitCancelled: true })).toBe('cancelled')
  })

  it('completed via explicit task_completed event', () => {
    expect(deriveTaskRunState({ taskStatus: 'running', explicitCompleted: true })).toBe('completed')
  })

  it('unknown / created / planning → running', () => {
    expect(deriveTaskRunState({})).toBe('running')
    expect(deriveTaskRunState({ taskStatus: 'created' })).toBe('running')
    expect(deriveTaskRunState({ taskStatus: 'generating' })).toBe('running')
  })
})
