/**
 * Phase 2.9A.35 — 真实 E2E 数据驱动的 reducer 验证。
 *
 * 通过真实后端 API(需要运行中的后端 + 有效 token)拉取
 * conv_7fe5806c / task_ed72a8ab 的真实 event-list,喂给前端 reducer,
 * 断言 hydrate 结果符合产品契约。
 *
 * 运行前提:
 *  - 后端已启动(http://localhost:8000)
 *  - /tmp/ta_token.txt 存在有效 token(user_d1498116)
 *
 * 若环境不可用,测试标记为 skip(不影响 CI 基线)。
 */
import { describe, expect, it } from 'vitest'
import { reduceTaskEvents, createInitialTaskRunBlock } from './useTaskEventReducer'
import type { TaskRunBlock } from '@/types'

declare const require: (id: 'fs') => {
  existsSync(path: string): boolean
  readFileSync(path: string, encoding: 'utf-8'): string
}

const TASK_ID = 'task_ed72a8ab'

async function fetchRealEvents(): Promise<unknown[]> {
  const fs = require('fs')
  const token = fs.existsSync('./_e2e_token.txt') ? fs.readFileSync('./_e2e_token.txt', 'utf-8').trim() : ''
  if (!token) throw new Error('no token file — E2E skipped')
  const res = await fetch(`http://127.0.0.1:8000/api/agent/tasks/${TASK_ID}/event-list?limit=100`, {
    headers: { Authorization: `Bearer ${token}` }
  })
  const json = await res.json()
  if (json.code !== 0 || !json.data?.events) throw new Error(`event-list failed: ${json.code}`)
  return json.data.events
}

describe('Phase 2.9A.35 — 真实 E2E reducer', () => {
  it('hydrates real task_ed72a8ab events into correct TaskRunBlock', async () => {
    let events: unknown[]
    try {
      events = await fetchRealEvents()
    } catch (err) {
      console.warn('[E2E] backend unavailable, skipping', err)
      return
    }
    expect(events.length).toBeGreaterThanOrEqual(35)

    const block: TaskRunBlock = createInitialTaskRunBlock(TASK_ID, 'waiting_user_confirm' as never, 'hydrate', {
      startedAt: '2026-07-30T15:23:41+00:00'
    })
    const result = reduceTaskEvents(block, events as never, 'hydrate')

    // 4 个 Tool 全部 SUCCESS
    expect(result.toolExecutions).toHaveLength(4)
    const rp = result.toolExecutions.find((t) => t.toolName === 'RequirementParserTool')
    expect(rp?.status).toBe('success')
    expect(rp?.terminal).toBe(true)
    expect(rp?.durationMs).toBe(25120)

    // lastSseSequence = 34(不是 1000000001)
    expect(result.lastSseSequence).toBe(34)
    expect(result.lastSseSequence).not.toBe(1000000001)

    // 章节确认卡片 17 sections
    const confirm = result.messages.find((m) => m.type === 'section_confirm')
    expect(confirm).toBeDefined()
    expect(confirm?.confirmationId).toBe('confirm_976a1129')
    expect(confirm?.sections?.length).toBe(17)

    // status = waiting_user_confirm
    expect(result.status).toBe('waiting_user_confirm')
    expect(result.currentPhase).toBe('section_confirmation')
  })
})
