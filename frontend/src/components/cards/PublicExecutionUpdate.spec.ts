import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import PublicExecutionUpdate from './PublicExecutionUpdate.vue'
import type { PublicExecutionUpdate as PublicUpdate } from '@/types'

function makeUpdate(overrides: Partial<PublicUpdate> = {}): PublicUpdate {
  return {
    version: 1,
    kind: 'tool_result',
    level: 'success',
    headline: '需求文档解析完成',
    summary: '已完成需求文档解析',
    impact: '用于确定测试范围',
    nextAction: '接下来解析模板',
    details: ['识别章节 5 个', '关键表格 3 个'],
    source: 'template',
    dedupeKey: 'RequirementParserTool:tool_result:success',
    ...overrides
  }
}

async function render(update: PublicUpdate | undefined): Promise<string> {
  const app = createSSRApp({
    render: () => h(PublicExecutionUpdate, { update })
  })
  return await renderToString(app)
}

describe('PublicExecutionUpdate', () => {
  it('renders the headline, summary, impact, and next action', async () => {
    const html = await render(makeUpdate())
    expect(html).toContain('需求文档解析完成')
    expect(html).toContain('已完成需求文档解析')
    expect(html).toContain('用于确定测试范围')
    expect(html).toContain('接下来解析模板')
  })

  it('renders the details list when present', async () => {
    const html = await render(makeUpdate({ details: ['a', 'b', 'c'] }))
    expect(html.match(/<li/g)?.length ?? 0).toBe(3)
    expect(html).toContain('a')
    expect(html).toContain('b')
    expect(html).toContain('c')
  })

  it('does not render the detail list when empty', async () => {
    const html = await render(makeUpdate({ details: [] }))
    expect(html.match(/<li/g)).toBeNull()
  })

  it('renders nothing when update is undefined', async () => {
    const html = await render(undefined)
    expect(html).not.toContain('<section')
  })

  it('renders the headline when level is warning', async () => {
    const html = await render(makeUpdate({ level: 'warning', headline: '审查含建议' }))
    expect(html).toContain('public-update')
    expect(html).toContain('审查含建议')
  })

  it('renders the headline when level is retrying', async () => {
    const html = await render(makeUpdate({
      level: 'retrying',
      kind: 'tool_retry',
      headline: '正在第 2 次重试',
      dedupeKey: 'TestPlanGeneratorTool:tool_retry:schema_feedback:2'
    }))
    expect(html).toContain('public-update')
    expect(html).toContain('正在第 2 次重试')
  })

  it('hides impact and next action when they are empty', async () => {
    const html = await render(makeUpdate({ impact: '', nextAction: '' }))
    expect(html).not.toContain('影响：')
    expect(html).not.toContain('下一步：')
  })

  it('renders natural narrative text without fixed labels', async () => {
    const html = await render(makeUpdate({
      narrativeText: '我已经读完需求文档了，这次提取到 43 个章节和 7 张表格。',
      summary: '旧摘要',
      impact: '旧影响',
      nextAction: '旧下一步',
      details: ['旧详情']
    }))

    expect(html).toContain('我已经读完需求文档了')
    expect(html).not.toContain('影响：')
    expect(html).not.toContain('下一步：')
    expect(html).not.toContain('<li')
  })

  it('renders markdown inside natural narrative text safely', async () => {
    const html = await render(makeUpdate({
      narrativeText: [
        'I found **3 blockers** that need attention:',
        '',
        '- Invoice OCR confidence is below the review threshold.',
        '- Budget approval has no clear fallback owner.',
        '',
        '<script>alert(1)</script>'
      ].join('\n'),
      summary: '',
      impact: '',
      nextAction: '',
      details: []
    }))

    expect(html).toContain('<strong>3 blockers</strong>')
    expect(html).toContain('<ul>')
    expect(html).toContain('<li>Invoice OCR confidence is below the review threshold.</li>')
    expect(html).not.toMatch(/<script>alert/)
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
  })

  it('renders preparation observation and next steps as compact ordered markdown', async () => {
    const html = await render(makeUpdate({
      narrativeText: [
        '### 观察',
        '1. 需求存在 3 项关键待确认规则。',
        '2. 当前没有可用知识库依据。',
        '',
        '### 下一步：需要你确认',
        '请在下方补充卡中完成 3 项关键决策：',
        '1. 访客类型与角色范围',
        '2. 预约审批与异常处理规则',
        '3. 到访核验方式与验收口径'
      ].join('\n'),
      summary: '',
      impact: '',
      nextAction: '',
      details: []
    }))

    expect(html).toContain('<h3>观察</h3>')
    expect(html).toContain('<h3>下一步：需要你确认</h3>')
    expect(html.match(/<ol>/g)?.length ?? 0).toBe(2)
    expect(html).not.toContain('<ul>')
  })

  it('marks unfinished narrative chunks as streaming for typewriter rendering', async () => {
    const html = await render(makeUpdate({
      narrativeText: 'I am reading the parser result now',
      chunkFinal: false,
      summary: '',
      impact: '',
      nextAction: '',
      details: []
    }))

    expect(html).toContain('public-update--streaming')
    expect(html).toContain('public-update__cursor')
  })

  it('escapes potentially malicious content (no script tag rendered)', async () => {
    const html = await render(makeUpdate({ headline: '<script>alert(1)</script>需求' }))
    expect(html).not.toMatch(/<script>alert/)
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('需求')
  })
})
