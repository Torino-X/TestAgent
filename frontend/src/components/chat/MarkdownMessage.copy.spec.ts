import { describe, expect, it } from 'vitest'

import messageListSource from './ChatMessageList.vue?raw'
import source from './MarkdownMessage.vue?raw'

describe('MarkdownMessage fenced-code copy interaction', () => {
  it('delegates copy clicks and copies the complete decoded code text', () => {
    expect(source).toContain('@click="handleContentClick"')
    expect(source).toContain("closest<HTMLButtonElement>('[data-code-copy]')")
    expect(source).toContain("querySelector<HTMLElement>('code')")
    expect(source).toContain('const codeText = codeElement.textContent ??')
    expect(source).toContain('await copyMarkdownText(codeText)')
  })

  it('shows copied feedback and keeps failures observable', () => {
    expect(source).toContain("button.classList.add('markdown-code-block__copy--copied')")
    expect(source).toContain("button.setAttribute('aria-label', '代码已复制')")
    expect(source).toContain("emit('toast', { kind: 'error', text: '复制失败，请手动复制' })")
    expect(messageListSource).toContain('@toast="emit(\'toast\', $event)"')
  })

  it('uses a light neutral surface instead of the old dark code card', () => {
    expect(source).toContain('background: #f4f4f4;')
    expect(source).toContain('color: #242424;')
    expect(source).toContain('.markdown-code-block__copy')
    expect(source).not.toContain('background: #0f172a;')
    expect(source).not.toContain('color: #e5edf8;')
  })
})
