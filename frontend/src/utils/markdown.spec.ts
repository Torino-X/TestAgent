import { describe, expect, it } from 'vitest'
import { renderMarkdown } from './markdown'

describe('renderMarkdown', () => {
  it('renders common chat markdown while escaping unsafe html', () => {
    const html = renderMarkdown(`# 标题

这是一段 **重点**，包含 \`code\` 和 [官网](https://example.com)。

- 第一项
- 第二项

> 引用内容

\`\`\`ts
const answer = '<script>alert(1)</script>'
\`\`\`

<img src=x onerror=alert(1)>`)

    expect(html).toContain('<h1>标题</h1>')
    expect(html).toContain('<strong>重点</strong>')
    expect(html).toContain('<code>code</code>')
    expect(html).toContain('<a href="https://example.com" target="_blank" rel="noopener noreferrer">官网</a>')
    expect(html).toContain('<ul>')
    expect(html).toContain('<li>第一项</li>')
    expect(html).toContain('<blockquote>引用内容</blockquote>')
    expect(html).toContain('<pre><code class="language-ts">')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img')
  })

  it('drops unsafe javascript links', () => {
    const html = renderMarkdown('[危险](javascript:alert(1))')

    expect(html).toContain('危险')
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('<a ')
  })

  it('keeps incomplete streamed headings renderable until their content arrives', () => {
    for (const partialHeading of ['## ', '## \r']) {
      expect(() => renderMarkdown(partialHeading)).not.toThrow()
      expect(renderMarkdown(partialHeading)).toContain('<p>')
    }

    expect(renderMarkdown('## Streaming heading')).toContain('<h2>Streaming heading</h2>')
  })

  it('auto-splits a long single-paragraph Chinese narrative into multiple <p> blocks', () => {
    // Phase 2.9A.X: LLM 输出 NARRATIVE 时经常写成单段连续文字,
    // 缺 \n\n 分段。这里验证 renderMarkdown 入口预处理能兜底切分。
    // 阈值 240,造 280+ 字长段
    const longNarrative = [
      '任务已完成，产物文件 task_001_测试方案.docx 已生成并可供使用。',
      '本次共生成 16 个章节，保留了 1 个模板原有章节，但审查发现仍存在 4 项阻断问题和 1 项建议需要人工确认处理。',
      '主要阻断问题包括：section_2 第 1 个表格第 1 行缺少表头：签名；',
      'section_2 第 1 个表格第 1 行包含非模板表头：TEST_FAULT_签名；',
      '缺少必含章节：修订记录、风险分析及测试交付物。',
      '此外，系统建议检查生成的测试方案是否包含所有必要章节，由于 RepairAgent 因调用异常被跳过，上述问题未自动修复，请在评审时重点关注。',
    ].join('')

    expect(longNarrative.length).toBeGreaterThan(240)
    expect(longNarrative).not.toContain('\n\n')

    const html = renderMarkdown(longNarrative)
    // 应该被切成多个 <p> 标签
    const pCount = (html.match(/<p>/g) ?? []).length
    expect(pCount).toBeGreaterThan(1)
  })

  it('preserves explicit \\n\\n from LLM (does not double-split)', () => {
    const explicit = '第一段内容。\n\n第二段内容。'
    const html = renderMarkdown(explicit)
    const pCount = (html.match(/<p>/g) ?? []).length
    expect(pCount).toBe(2)
  })

  it('does not split short single-paragraph text', () => {
    // 短文本(< 240)不强制切分,避免破坏短回答
    const short = '简短回答。'
    const html = renderMarkdown(short)
    expect((html.match(/<p>/g) ?? []).length).toBe(1)
  })

  it('does not split numeric decimals like 3.5 in Chinese prose', () => {
    // 数字 / 小数点 + 数字不应被切成新段
    const withNumber = '本次评分 3.5 分。下一段内容。'.padEnd(260, '。后续填充文本。')
    const html = renderMarkdown(withNumber)
    // "3.5" 必须保持完整,不能被误切
    expect(html).toContain('3.5')
  })

  it('renders fenced code with a language header and accessible copy action', () => {
    const html = renderMarkdown(`\`\`\`python
print("<完整内容>")
print("第二行")
\`\`\``)

    expect(html).toContain('<figure class="markdown-code-block">')
    expect(html).toContain('<figcaption class="markdown-code-block__header">')
    expect(html).toContain('<span class="markdown-code-block__language">python</span>')
    expect(html).toContain('data-code-copy')
    expect(html).toContain('aria-label="复制代码"')
    expect(html).toContain('<pre><code class="language-python">')
    expect(html).toContain('print(&quot;&lt;完整内容&gt;&quot;)\nprint(&quot;第二行&quot;)')
  })

  it('uses a neutral fallback label for an untyped fenced block', () => {
    const html = renderMarkdown('```\nvalue = 1\n```')

    expect(html).toContain('<span class="markdown-code-block__language">代码</span>')
  })
})
