/**
 * Timeline 锚定工具 — AgentRunCard 把 narrative / format-loss banner
 * 插入 timeline 已有节点时，按 order 找锚点位置的纯函数。
 *
 * BUG FIX 2026-08-19：format-loss banner 必须锚定到 DocxFormatCheckTool
 * 完成项之后，而不是渲染在 timeline 顶部（用户视角）。
 */

import type { ChatMessage, DynamicNarrative } from '@/types'

/** 与 AgentRunCard.vue 的 TimelineItem 形态对齐（最小子集）。 */
export interface TimelineAnchorCandidate {
  kind: 'tool' | 'narrative' | 'step' | 'confirmation' | 'confirmation-receipt' | 'section-strategy' | 'preparation-clarification' | 'format-loss' | 'template-identified'
  message?: ChatMessage
  narrative?: DynamicNarrative
}

export interface TimelineInsertItem {
  /** 给 helper 用的最小 kind 字段即可；真实 item 会带更多字段。 */
  kind: TimelineAnchorCandidate['kind']
  message?: ChatMessage
  narrative?: DynamicNarrative
}

/**
 * 把新 item 按 order 锚定到已有 items 数组中。
 * 规则：找到最后一个 order ≤ newOrder 的 item，插到它后面；
 * 若 newOrder 缺失或所有 item.order 都 > newOrder，append 到末尾。
 */
export function insertAtOrder<T extends TimelineInsertItem>(
  items: T[],
  newItem: T,
  newOrder: number | undefined,
): void {
  if (typeof newOrder !== 'number') {
    items.push(newItem)
    return
  }
  let insertAt = items.length
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const candidate = items[i]
    const itemOrder = orderOf(candidate)
    if (typeof itemOrder === 'number' && itemOrder <= newOrder) {
      insertAt = i + 1
      break
    }
  }
  items.splice(insertAt, 0, newItem)
}

/** 从候选 item 抽取 order（tool/message.conversationSequence 或 narrative.order）。 */
export function orderOf(candidate: TimelineAnchorCandidate): number | undefined {
  if (candidate.kind === 'tool' && candidate.message) {
    return typeof candidate.message.conversationSequence === 'number'
      ? candidate.message.conversationSequence
      : undefined
  }
  if (candidate.kind === 'confirmation-receipt' && candidate.message) {
    return typeof candidate.message.conversationSequence === 'number'
      ? candidate.message.conversationSequence
      : undefined
  }
  if (candidate.kind === 'narrative' && candidate.narrative) {
    return candidate.narrative.canonicalOrder ?? candidate.narrative.sequenceNo ?? undefined
  }
  return undefined
}

/**
 * 选 format-loss banner 的锚点 order。
 *
 * 规则：
 *   1. 优先取 DocxFormatCheckTool 工具项的 conversationSequence；
 *   2. 若 format-loss 消息自带 conversationSequence 也参与对齐；
 *   3. 取两者中较大的（保证 banner 落在自检工具完成之后、且不会被旧消息
 *      插到它前面）；
 *   4. 都缺失时返回 undefined（调用方 append 到末尾）。
 */
export function formatLossAnchorOrder(
  checkToolOrder: number | undefined,
  lossMessageOrder: number | undefined,
): number | undefined {
  if (typeof checkToolOrder === 'number' && typeof lossMessageOrder === 'number') {
    return Math.max(checkToolOrder, lossMessageOrder)
  }
  if (typeof checkToolOrder === 'number') return checkToolOrder
  if (typeof lossMessageOrder === 'number') return lossMessageOrder
  return undefined
}
