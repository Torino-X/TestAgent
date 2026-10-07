/**
 * Phase 2.9A.35 — 统一事件/确认/时间规范化工具。
 *
 * 职责:
 *  1. normalizeTaskEventPayload — event payload 恒为 Record<string, unknown>。
 *     兼容 dict / JSON string / None / 非法 JSON。
 *  2. normalizeConfirmationSections — 唯一确认字段解析入口。
 *     兼容 payload.sections / payload.section_suggestions /
 *     payload.section_suggestions.sections。
 *  3. extractPublicExecutionUpdate — 唯一 PublicExecutionUpdate 解析入口。
 *     兼容 v3 扁平 payload / publicUpdate / public_update /
 *     public_execution_update / publicExecutionUpdate 五种契约。
 *  4. parseApiDateTime — RFC3339 UTC 或无时区 "YYYY-MM-DD HH:mm:ss"
 *     一律按 UTC 解析,绝不交给浏览器按本地时区解释。
 *
 * 消费点(useTaskEventReducer / useTaskEventRestore / useTaskEvents / taskTiming)
 * 必须复用本模块,不得各自维护字段读取。
 */
import type { PublicExecutionUpdate, SectionItem } from '@/types'

/** 把任意 event payload 规范化为 Record<string, unknown>。 */
export function normalizeTaskEventPayload(value: unknown): Record<string, unknown> {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    return { ...(value as Record<string, unknown>) }
  }
  if (typeof value === 'string' && value.trim()) {
    try {
      const parsed: unknown = JSON.parse(value)
      if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>
      }
    } catch {
      // 非法 JSON → 返回空对象
    }
  }
  return {}
}

/**
 * 从 confirmation payload 提取 section 数组(唯一入口)。
 * 兼容三种字段形态:
 *  - payload.sections
 *  - payload.section_suggestions(对象,内含 .sections)
 *  - payload.section_suggestions.sections(嵌套两层)
 */
export function normalizeConfirmationSections(payload: Record<string, unknown>): unknown[] {
  const topLevel = payload.sections
  if (Array.isArray(topLevel)) return topLevel

  const suggestion = payload.section_suggestions
  if (suggestion !== null && typeof suggestion === 'object' && !Array.isArray(suggestion)) {
    const nested = (suggestion as Record<string, unknown>).sections
    if (Array.isArray(nested)) return nested
  }

  // 兼容 section_suggestions.sections 嵌套两层
  if (suggestion !== null && typeof suggestion === 'object' && !Array.isArray(suggestion)) {
    const inner = (suggestion as Record<string, unknown>).section_suggestions
    if (inner !== null && typeof inner === 'object' && !Array.isArray(inner)) {
      const nested = (inner as Record<string, unknown>).sections
      if (Array.isArray(nested)) return nested
    }
  }

  return []
}

/** 从 confirmation payload 提取 section 数组并映射为 SectionItem[]。 */
export function normalizeConfirmationSectionsItems(payload: Record<string, unknown>): SectionItem[] {
  return normalizeConfirmationSections(payload).map(mapSectionItem)
}

function mapSectionItem(raw: unknown, index: number): SectionItem {
  const section = (raw !== null && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const getString = (keys: string[], fallback = ''): string => {
    for (const key of keys) {
      const v = section[key]
      if (typeof v === 'string' && v) return v
    }
    return fallback
  }
  const suggestedAction = getString(['suggested_action', 'suggestedAction'], 'ai_generate') as SectionItem['suggestedAction']
  return {
    id: getString(['section_id', 'id'], `section_${index}`),
    code: getString(['section_number', 'code'], String(index + 1)),
    title: getString(['section_title', 'title'], `章节 ${index + 1}`),
    level: getString(['level'], 'H1'),
    suggestedAction,
    action: getString(['action'], suggestedAction) as SectionItem['action'],
    reason: getString(['reason', 'description']),
    constraintSource: getString(['constraint_source', 'constraintSource']) === 'user_prompt' ? 'user_prompt' : undefined
  }
}

function toSafeString(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}

function pickFirst(record: Record<string, unknown>, keys: string[], fallback = ''): string {
  for (const key of keys) {
    const value = toSafeString(record[key])
    if (value) return value
  }
  return fallback
}

function toSafeInt(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value)
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return Math.trunc(parsed)
  }
  return fallback
}

function toSafeDetails(value: unknown): string[] {
  // Phase 2.9B.3: 正式合同 details 为 string[]。
  // 历史事件可能含 dict(旧 AgentPublicUpdateDraft)或 null/非法值:
  //   - string[] → 原样清洗(trim、删空、最多 5 项);
  //   - dict     → 每键值对转 "键: 值"(历史兼容,不改 UI);
  //   - null / 非法 → []。
  if (Array.isArray(value)) {
    return value
      .map((item) => toSafeString(item).trim())
      .filter(Boolean)
      .slice(0, 5)
  }
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>
    return Object.entries(record)
      .map(([key, item]) => {
        const k = toSafeString(key).trim()
        const v = toSafeString(item).trim()
        return k ? (v ? `${k}: ${v}` : k) : ''
      })
      .filter(Boolean)
      .slice(0, 5)
  }
  return []
}

/**
 * 唯一 PublicExecutionUpdate 解析入口(Phase 2.9A.32 Canonical)。
 *
 * 契约解析优先级:
 *  1. 显式嵌套对象: public_execution_update / publicExecutionUpdate /
 *     publicUpdate / public_update(若存在且为普通对象)。
 *  2. 无嵌套对象时,尝试把当前 payload 识别为扁平 PublicExecutionUpdate
 *     (LangGraph v3 真实结构,headline 等字段位于顶层)。
 *
 * 运行时校验:
 *  - 输入先经 normalizeTaskEventPayload 规范化,绝不直接强转 unknown。
 *  - headline 必须是非空字符串,否则返回 undefined。
 *  - summary/impact/nextAction/details/kind/level/source/dedupeKey 做安全转换,
 *    单个可选字段类型异常不会导致整条 update 丢失。
 *  - 不修改原始 payload 对象。
 */
export function extractPublicExecutionUpdate(payload: unknown): PublicExecutionUpdate | undefined {
  const record = normalizeTaskEventPayload(payload)
  if (Object.keys(record).length === 0) return undefined

  const nestedKeys = ['public_execution_update', 'publicExecutionUpdate', 'publicUpdate', 'public_update'] as const
  let source: Record<string, unknown> | undefined
  for (const key of nestedKeys) {
    const candidate = record[key]
    if (candidate !== null && typeof candidate === 'object' && !Array.isArray(candidate)) {
      source = candidate as Record<string, unknown>
      break
    }
  }
  const resolved = source ?? record

  const headline = pickFirst(resolved, ['headline'])
  if (!headline) return undefined

  return {
    version: toSafeInt(resolved.version, 1),
    kind: (pickFirst(resolved, ['kind'], 'tool_result') as PublicExecutionUpdate['kind']),
    level: (pickFirst(resolved, ['level'], 'info') as PublicExecutionUpdate['level']),
    headline,
    summary: pickFirst(resolved, ['summary']),
    impact: pickFirst(resolved, ['impact']),
    nextAction: pickFirst(resolved, ['next_action', 'nextAction']),
    details: toSafeDetails(resolved.details),
    narrativeText: pickFirst(resolved, ['narrative_text', 'narrativeText']),
    source: (pickFirst(resolved, ['source'], 'template') as PublicExecutionUpdate['source']),
    dedupeKey: pickFirst(resolved, ['dedupe_key', 'dedupeKey']),
    chunkIndex: toSafeInt(resolved.chunk_index ?? resolved.chunkIndex, 0),
    chunkTotal: toSafeInt(resolved.chunk_total ?? resolved.chunkTotal, 1),
    chunkFinal: typeof resolved.chunk_final === 'boolean'
      ? resolved.chunk_final
      : typeof resolved.chunkFinal === 'boolean'
        ? resolved.chunkFinal
        : true
  }
}

/**
 * 字段级单调 merge 两个 PublicExecutionUpdate。
 *
 * 后端 builder 的 `split_into_chunks` 产生「累计快照」分帧:
 * frame0=headline, frame1=+summary, frame2=+impact, frame3=+next_action,
 * frame4=+details+chunkFinal。逐帧使用非空字段覆盖,保证:
 *  - 乱序重放时后到的不完整帧不会清空已有字段;
 *  - 后续空帧(无叙事)不会覆盖前面的完整 update;
 *  - terminal 帧为空时保留已有完整内容。
 */
export function mergePublicExecutionUpdate(
  base: PublicExecutionUpdate | undefined,
  incoming: PublicExecutionUpdate | undefined
): PublicExecutionUpdate | undefined {
  if (!base) return incoming
  if (!incoming) return base
  return {
    ...base,
    headline: incoming.headline || base.headline,
    summary: incoming.summary || base.summary,
    impact: incoming.impact || base.impact,
    nextAction: incoming.nextAction || base.nextAction,
    details: incoming.details.length > 0 ? incoming.details : base.details,
    narrativeText: incoming.narrativeText || base.narrativeText,
    kind: incoming.kind || base.kind,
    level: incoming.level || base.level,
    source: incoming.source || base.source,
    dedupeKey: incoming.dedupeKey || base.dedupeKey,
    chunkIndex: incoming.chunkIndex ?? base.chunkIndex,
    chunkTotal: incoming.chunkTotal ?? base.chunkTotal,
    chunkFinal: incoming.chunkFinal ?? base.chunkFinal
  }
}

/**
 * 解析后端时间字符串为 UTC epoch ms。
 *  - 带时区后缀("...Z" 或 "+00:00")→ 标准 Date.parse
 *  - 无时区 "YYYY-MM-DD HH:mm:ss" → 明确按 UTC 解析
 *  - 非法 → NaN
 */
export function formatPublicExecutionUpdateText(update: PublicExecutionUpdate | undefined): string {
  if (!update || !update.headline) return ''
  if (update.narrativeText?.trim()) return update.narrativeText.trim()
  const lines = [
    update.headline,
    update.summary,
    update.impact ? `影响：${update.impact}` : '',
    update.nextAction ? `下一步：${update.nextAction}` : '',
    ...(Array.isArray(update.details) ? update.details.map((detail) => detail ? `· ${detail}` : '') : [])
  ]
  return lines.map((line) => line.trim()).filter(Boolean).join('\n')
}

export function parseApiDateTime(value?: string | null): number {
  if (!value) return NaN
  const raw = value.trim()
  if (!raw) return NaN

  // 无时区后缀(空格分隔的 "YYYY-MM-DD HH:mm:ss")
  if (!/[zZ]$/.test(raw) && !/[+-]\d{2}:?\d{2}$/.test(raw)) {
    const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/)
    if (m) {
      const [, y, mo, d, h, mi, s] = m.map(Number)
      const date = new Date(Date.UTC(y, mo - 1, d, h, mi, s))
      return date.getTime()
    }
  }

  const parsed = Date.parse(raw)
  return Number.isFinite(parsed) ? parsed : NaN
}

/** 兼容历史数据的服务器时间解析(与 parseApiDateTime 语义一致)。 */
export function parseApiDate(value?: string | null): Date {
  const ms = parseApiDateTime(value)
  return Number.isFinite(ms) ? new Date(ms) : new Date(NaN)
}
