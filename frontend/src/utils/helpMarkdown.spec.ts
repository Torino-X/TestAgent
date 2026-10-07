import { describe, expect, it } from 'vitest'
import { headingAnchor, renderHelpMarkdown } from './helpMarkdown'

describe('Help Markdown renderer', () => {
  it('uses stable Unicode anchors with duplicate suffixes', () => {
    const seen = new Map<string, number>()
    expect(headingAnchor('准备需求文档', seen)).toBe('准备需求文档')
    expect(headingAnchor('准备需求文档', seen)).toBe('准备需求文档-2')
  })

  it('disables raw HTML and supports callouts, Mermaid, and internal links', () => {
    const html = renderHelpMarkdown(`
<script>alert('x')</script>

## 流程

:::tip
先检查输入。
:::

\`\`\`mermaid
flowchart LR
A --> B
\`\`\`

[继续](/help/article/next)
`)

    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('id="流程"')
    expect(html).toContain('help-callout--tip')
    expect(html).toContain('class="help-mermaid"')
    expect(html).toContain('data-help-link')
  })

  it('rejects unsafe image protocols', () => {
    const html = renderHelpMarkdown('![x](javascript:alert(1))')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('javascript:')
  })

  it('accepts images only from the mounted Help asset route', () => {
    const html = renderHelpMarkdown('![流程图](/api/help/assets/workflow.png)')
    expect(html).toContain('<img src="/api/help/assets/workflow.png"')
  })
})
