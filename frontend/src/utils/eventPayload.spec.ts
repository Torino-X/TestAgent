/**
 * Phase 2.9A.32 — extractPublicExecutionUpdate Canonical 解析器单元测试
 *
 * 覆盖契约:
 *  - v3 扁平 payload(生产真实结构)
 *  - publicUpdate / public_update / public_execution_update /
 *    publicExecutionUpdate 四种嵌套结构
 *  - JSON 字符串 / null / undefined / 非法 JSON / 数组 / 数字 / 布尔
 *  - headline 校验与可选字段安全规范化
 *  - 嵌套优先于扁平 / 不修改原始 payload / 2.9B public_update 兼容
 */
import { describe, it, expect } from 'vitest'
import {
  extractPublicExecutionUpdate,
  formatPublicExecutionUpdateText,
  mergePublicExecutionUpdate,
  normalizeTaskEventPayload
} from '@/utils/eventPayload'

describe('extractPublicExecutionUpdate — Canonical 解析器', () => {
  it('解析 v3 扁平 payload(生产真实结构)', () => {
    const update = extractPublicExecutionUpdate({
      headline: '需求文档解析完成',
      summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。',
      impact: '解析结果将用于确定测试范围以及测试方案章节结构。',
      next_action: '接下来将解析测试方案模板。',
      details: ['识别章节 43 个，关键表格 7 个', '含 3 张图片的关键文本'],
      kind: 'tool_result',
      level: 'success',
      source: 'template',
      dedupe_key: 'RequirementParserTool:tool_result:success',
      chunk_index: 4,
      chunk_total: 5,
      chunk_final: true,
      tool_name: 'RequirementParserTool',
      duration_ms: 1234
    })
    expect(update).toBeDefined()
    expect(update?.headline).toBe('需求文档解析完成')
    expect(update?.summary).toContain('需求文档解析')
    expect(update?.impact).toContain('测试范围')
    expect(update?.nextAction).toBe('接下来将解析测试方案模板。')
    expect(update?.details).toEqual(['识别章节 43 个，关键表格 7 个', '含 3 张图片的关键文本'])
    expect(update?.kind).toBe('tool_result')
    expect(update?.level).toBe('success')
    expect(update?.source).toBe('template')
    expect(update?.dedupeKey).toBe('RequirementParserTool:tool_result:success')
    expect(update?.chunkIndex).toBe(4)
    expect(update?.chunkTotal).toBe(5)
    expect(update?.chunkFinal).toBe(true)
    // 普通 tool 字段不阻塞扁平识别
  })

  it('解析 publicUpdate 嵌套(camelCase)', () => {
    const update = extractPublicExecutionUpdate({
      publicUpdate: {
        version: 1,
        kind: 'tool_result',
        level: 'success',
        headline: '测试方案模板解析完成',
        summary: 's',
        impact: 'i',
        nextAction: 'na',
        details: [],
        source: 'template',
        dedupeKey: 'k'
      },
      tool_name: 'TemplateParserTool'
    })
    expect(update?.headline).toBe('测试方案模板解析完成')
    expect(update?.nextAction).toBe('na')
    expect(update?.dedupeKey).toBe('k')
  })

  it('解析 public_update 嵌套(snake_case, 2.9B 兼容)', () => {
    const update = extractPublicExecutionUpdate({
      public_update: {
        headline: 'Preparation 决策',
        summary: '准备生成测试方案',
        impact: '影响下一步',
        next_action: '继续生成',
        details: ['detail 1']
      },
      tool_name: 'PreparationAgent'
    })
    expect(update?.headline).toBe('Preparation 决策')
    expect(update?.nextAction).toBe('继续生成')
    expect(update?.details).toEqual(['detail 1'])
  })

  it('解析 narrative_text 并作为优先展示文本', () => {
    const update = extractPublicExecutionUpdate({
      public_update: {
        headline: '需求解析完成',
        narrative_text: '我已经读完需求文档了，这次提取到 43 个章节和 7 张表格，后续可以直接沿着这些业务结构生成测试方案。',
        summary: '',
        impact: '',
        next_action: '',
        details: []
      }
    })

    expect(update?.narrativeText).toContain('我已经读完需求文档了')
    expect(formatPublicExecutionUpdateText(update)).toBe(update?.narrativeText)
  })

  it('解析 public_execution_update 嵌套(Legacy/v2)', () => {
    const update = extractPublicExecutionUpdate({
      public_execution_update: {
        headline: '知识库检索完成',
        summary: '已检索',
        impact: '影响',
        next_action: '下一步',
        details: ['命中 3 条']
      },
      tool_name: 'KnowledgeSearchTool'
    })
    expect(update?.headline).toBe('知识库检索完成')
    expect(update?.details).toEqual(['命中 3 条'])
  })

  it('解析 publicExecutionUpdate 嵌套(camelCase 固定叙事别名)', () => {
    const update = extractPublicExecutionUpdate({
      publicExecutionUpdate: { headline: 'Word 文档导出完成', summary: '', impact: '', nextAction: '', details: [] }
    })
    expect(update?.headline).toBe('Word 文档导出完成')
  })

  it('解析 JSON 字符串 payload', () => {
    const update = extractPublicExecutionUpdate(JSON.stringify({
      headline: '章节处理建议已生成',
      summary: '建议已生成',
      next_action: '请确认'
    }))
    expect(update?.headline).toBe('章节处理建议已生成')
  })

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['非法 JSON 字符串', 'not-json{{{'],
    ['数组', []],
    ['数字', 42],
    ['布尔', true],
    ['空对象', {}],
    ['空字符串', '']
  ])('%s 返回 undefined', (_label, input) => {
    expect(extractPublicExecutionUpdate(input)).toBeUndefined()
  })

  it('无 headline 返回 undefined', () => {
    expect(extractPublicExecutionUpdate({ summary: '没有 headline' })).toBeUndefined()
  })

  it('空 headline 返回 undefined', () => {
    expect(extractPublicExecutionUpdate({ headline: '' })).toBeUndefined()
  })

  it('headline 非字符串(数字)不返回 undefined', () => {
    // 数字会被安全转换为字符串;只有完全缺失/空才拒绝
    const update = extractPublicExecutionUpdate({ headline: 123 })
    expect(update?.headline).toBe('123')
  })

  it('summary/impact 缺失时返回已定义 update 且为空字符串', () => {
    const update = extractPublicExecutionUpdate({ headline: '只有标题' })
    expect(update).toBeDefined()
    expect(update?.summary).toBe('')
    expect(update?.impact).toBe('')
    expect(update?.nextAction).toBe('')
    expect(update?.details).toEqual([])
  })

  it('next_action 转 Canonical nextAction', () => {
    const update = extractPublicExecutionUpdate({ headline: 'h', next_action: 'snake 行为' })
    expect(update?.nextAction).toBe('snake 行为')
  })

  it('nextAction camelCase 兼容', () => {
    const update = extractPublicExecutionUpdate({ headline: 'h', nextAction: 'camel 行为' })
    expect(update?.nextAction).toBe('camel 行为')
  })

  it('details 数组被规范化', () => {
    const update = extractPublicExecutionUpdate({ headline: 'h', details: ['a', 'b', 'c'] })
    expect(update?.details).toEqual(['a', 'b', 'c'])
  })

  it('details 为字符串/数字被安全处理为字符串, 非文本项被过滤', () => {
    const update = extractPublicExecutionUpdate({ headline: 'h', details: [1, 'two', true, null] })
    expect(update?.details).toEqual(['1', 'two'])
  })

  it('details 非数组时返回空数组', () => {
    const update = extractPublicExecutionUpdate({ headline: 'h', details: 'not-array' })
    expect(update?.details).toEqual([])
  })

  it('unrelated Tool payload 不被误识别', () => {
    expect(extractPublicExecutionUpdate({ tool_name: 'X', status: 'ok', duration_ms: 5 })).toBeUndefined()
    expect(extractPublicExecutionUpdate({ output: 'some output', input: 'some input' })).toBeUndefined()
  })

  it('嵌套结构优先于扁平结构', () => {
    const update = extractPublicExecutionUpdate({
      // 顶层扁平 headline 与嵌套 headline 同时存在 → 嵌套优先
      headline: '扁平标题',
      summary: '扁平摘要',
      public_execution_update: {
        headline: '嵌套标题',
        summary: '嵌套摘要',
        impact: '嵌套影响',
        next_action: '嵌套下一步',
        details: ['嵌套详情']
      }
    })
    expect(update?.headline).toBe('嵌套标题')
    expect(update?.summary).toBe('嵌套摘要')
  })

  it('不修改原始 payload 对象', () => {
    const original = {
      headline: '需求文档解析完成',
      summary: 's',
      next_action: 'na',
      details: ['a']
    }
    const before = JSON.stringify(original)
    extractPublicExecutionUpdate(original)
    expect(JSON.stringify(original)).toBe(before)
    // 不向原始对象写回 camelCase 字段
    expect('nextAction' in original).toBe(false)
  })

  it('camelCase chunk 字段兼容', () => {
    const update = extractPublicExecutionUpdate({
      headline: 'h',
      chunkIndex: 2,
      chunkTotal: 5,
      chunkFinal: false
    })
    expect(update?.chunkIndex).toBe(2)
    expect(update?.chunkTotal).toBe(5)
    expect(update?.chunkFinal).toBe(false)
  })

  it('normalizeTaskEventPayload 被复用(对象浅拷贝, 字符串解析)', () => {
    const original = { headline: 'h' }
    const norm = normalizeTaskEventPayload(original)
    expect(norm).not.toBe(original)
    expect(norm.headline).toBe('h')
    expect(normalizeTaskEventPayload('{"headline":"s"}').headline).toBe('s')
    expect(normalizeTaskEventPayload('bad{').headline).toBeUndefined()
  })
})

describe('mergePublicExecutionUpdate — 字段级单调 merge', () => {
  it('base 为空时返回 incoming', () => {
    const incoming = extractPublicExecutionUpdate({ headline: 'h', summary: 's' })
    expect(mergePublicExecutionUpdate(undefined, incoming)).toBe(incoming)
  })

  it('incoming 为空时返回 base', () => {
    const base = extractPublicExecutionUpdate({ headline: 'h' })
    expect(mergePublicExecutionUpdate(base, undefined)).toBe(base)
  })

  it('空字段帧不清空已有字段', () => {
    const base = extractPublicExecutionUpdate({
      headline: 'h', summary: 's', impact: 'i', next_action: 'na', details: ['d']
    })!
    const emptyFrame = extractPublicExecutionUpdate({ headline: 'h', summary: '', impact: '', next_action: '' })!
    const merged = mergePublicExecutionUpdate(base, emptyFrame)!
    expect(merged.summary).toBe('s')
    expect(merged.impact).toBe('i')
    expect(merged.nextAction).toBe('na')
    expect(merged.details).toEqual(['d'])
  })

  it('乱序帧逐字段补全不丢失', () => {
    // 真实 builder split_into_chunks 分帧是「累计快照」:
    // frame2(+impact) 先到,frame0(headline) 后到 → 逐字段单调 merge
    const frame2 = extractPublicExecutionUpdate({
      headline: '需求文档解析完成', summary: 's', impact: 'i', next_action: '', details: []
    })!
    const frame0 = extractPublicExecutionUpdate({
      headline: '需求文档解析完成', summary: '', impact: '', next_action: '', details: []
    })!
    const merged = mergePublicExecutionUpdate(frame2, frame0)!
    expect(merged.headline).toBe('需求文档解析完成')
    expect(merged.summary).toBe('s')
    expect(merged.impact).toBe('i')
  })

  it('非空新值覆盖旧值, 空值保留旧值', () => {
    const base = extractPublicExecutionUpdate({ headline: 'h', summary: 'old' })!
    const incoming = extractPublicExecutionUpdate({ headline: 'h2', summary: '', impact: 'new-impact' })!
    const merged = mergePublicExecutionUpdate(base, incoming)!
    expect(merged.headline).toBe('h2')
    expect(merged.summary).toBe('old')
    expect(merged.impact).toBe('new-impact')
  })
})

describe('formatPublicExecutionUpdateText', () => {
  it('formats the full LLM task summary instead of only summary', () => {
    const update = extractPublicExecutionUpdate({
      headline: 'Campus booking test plan completed',
      summary: 'Generated the test plan from the requirement document and template.',
      impact: 'The output can be used as the project test baseline.',
      next_action: 'Download the generated Word artifact for review.',
      details: [
        'Generated 16 test-plan sections.',
        'Found 3 blocking review issues.'
      ]
    })

    const text = formatPublicExecutionUpdateText(update)

    expect(text).toContain('Campus booking test plan completed')
    expect(text).toContain('Generated the test plan from the requirement document and template.')
    expect(text).toContain('影响：The output can be used as the project test baseline.')
    expect(text).toContain('下一步：Download the generated Word artifact for review.')
    expect(text).toContain('· Generated 16 test-plan sections.')
    expect(text).toContain('· Found 3 blocking review issues.')
  })

  it('prefers natural narrative text over the legacy formatted fields', () => {
    const update = extractPublicExecutionUpdate({
      headline: '需求解析完成',
      narrative_text: '我已经读完需求文档了，这次提取到 43 个章节和 7 张表格。',
      summary: '旧摘要',
      impact: '旧影响',
      next_action: '旧下一步',
      details: ['旧详情']
    })

    const text = formatPublicExecutionUpdateText(update)

    expect(text).toBe('我已经读完需求文档了，这次提取到 43 个章节和 7 张表格。')
    expect(text).not.toContain('影响：')
    expect(text).not.toContain('下一步：')
  })
})
