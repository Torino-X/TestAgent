/** BUG FIX 2026-08-19 — timeline anchor utility 回归测试。

覆盖:
  1. insertAtOrder: 把新 item 锚定到最后一个 order ≤ newOrder 的已有 item 之后
  2. insertAtOrder: newOrder 缺失 → append 到末尾
  3. insertAtOrder: 工具项 order = message.conversationSequence
  4. insertAtOrder: narrative order = canonicalOrder ?? sequenceNo
  5. formatLossAnchorOrder: check_tool_order 和 loss_message_order 都存在 → 取 max
  6. formatLossAnchorOrder: 只有 check_tool_order → 返回它
  7. formatLossAnchorOrder: 都缺失 → 返回 undefined（调用方 fallback 到末尾）
*/

import { describe, expect, it } from 'vitest'
import type { ChatMessage, DynamicNarrative } from '@/types'
import {
  formatLossAnchorOrder,
  insertAtOrder,
  orderOf,
  type TimelineInsertItem
} from './timelineAnchor'

function toolMessage(sequence: number, id: string = `tool_${sequence}`): ChatMessage {
  return {
    id,
    type: 'tool_call',
    role: 'agent',
    taskId: 'task_test',
    conversationSequence: sequence,
    toolCall: { id, name: 'X', status: 'done' }
  } as unknown as ChatMessage
}

function narrative(canonicalOrder: number, id: string = `nar_${canonicalOrder}`): DynamicNarrative {
  return {
    id,
    eventType: 'agent_decision_update',
    sourceEventId: id,
    canonicalOrder,
    sequenceNo: canonicalOrder + 1000,
    agentName: 'A'
  } as unknown as DynamicNarrative
}

describe('insertAtOrder', () => {
  it('anchors new item after the last existing item with order ≤ newOrder', () => {
    const items: TimelineInsertItem[] = [
      { kind: 'tool', message: toolMessage(5) },
      { kind: 'tool', message: toolMessage(8) },
      { kind: 'tool', message: toolMessage(12) }
    ]
    const newItem: TimelineInsertItem = {
      kind: 'tool',
      message: toolMessage(10, 'loss_marker')
    }
    insertAtOrder(items, newItem, 10)
    // 应插在 order=8 之后、order=12 之前 → 索引 2
    expect(items.map((it) => orderOf(it))).toEqual([5, 8, 10, 12])
  })

  it('appends to the end when newOrder is undefined', () => {
    const items: TimelineInsertItem[] = [
      { kind: 'tool', message: toolMessage(5) }
    ]
    insertAtOrder(items, { kind: 'step' }, undefined)
    expect(items).toHaveLength(2)
    expect(items[1].kind).toBe('step')
  })

  it('appends to the end when all existing items have order > newOrder', () => {
    const items: TimelineInsertItem[] = [
      { kind: 'tool', message: toolMessage(20) }
    ]
    insertAtOrder(items, { kind: 'step' }, 5)
    expect(items[1].kind).toBe('step')
  })

  it('reads tool order from message.conversationSequence', () => {
    const item: TimelineInsertItem = { kind: 'tool', message: toolMessage(42) }
    expect(orderOf(item)).toBe(42)
  })

  it('reads narrative order from canonicalOrder (falls back to sequenceNo)', () => {
    expect(orderOf({ kind: 'narrative', narrative: narrative(7) })).toBe(7)
  })
})

describe('formatLossAnchorOrder', () => {
  it('returns the max when both check and loss orders exist', () => {
    expect(formatLossAnchorOrder(12, 14)).toBe(14)
    expect(formatLossAnchorOrder(14, 12)).toBe(14)
  })

  it('returns check order when only check tool order exists', () => {
    expect(formatLossAnchorOrder(12, undefined)).toBe(12)
  })

  it('returns loss order when only loss message order exists', () => {
    expect(formatLossAnchorOrder(undefined, 9)).toBe(9)
  })

  it('returns undefined when both are missing', () => {
    expect(formatLossAnchorOrder(undefined, undefined)).toBeUndefined()
  })
})