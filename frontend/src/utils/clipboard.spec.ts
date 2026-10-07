/**
 * clipboard.ts — Markdown 复制工具
 *
 * 测试覆盖范围:
 * - navigator.clipboard.writeText 成功路径
 * - Markdown 原文保留(标题/列表/代码围栏/链接/粗体/斜体)
 * - 空/Null/Undefined 输入
 * - clipboard API 缺失时回退失败返回 'failed'(Node-only 环境下 document 也缺失)
 *
 * legacy textarea+execCommand 路径依赖浏览器 DOM,在 node-only 测试环境
 * 中走 'failed' 返回。该路径在真实浏览器/IDE 集成测试中验证。
 */

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { copyMarkdownText } from '@/utils/clipboard'

type RestoreHandle = () => void

function installNavigator(value: unknown): RestoreHandle {
  const g = globalThis as unknown as Record<string, unknown>
  const original = g.navigator
  Object.defineProperty(globalThis, 'navigator', {
    value,
    configurable: true,
    writable: true
  })
  return () => {
    if (original === undefined) {
      delete (globalThis as unknown as Record<string, unknown>).navigator
    } else {
      Object.defineProperty(globalThis, 'navigator', {
        value: original,
        configurable: true,
        writable: true
      })
    }
  }
}

describe('clipboard utility', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('returns success via navigator.clipboard.writeText', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    const restoreNav = installNavigator({ clipboard: { writeText } })
    try {
      const result = await copyMarkdownText('# 标题\n\n- 项目A\n- 项目B')
      expect(result).toBe('success')
      expect(writeText).toHaveBeenCalledWith('# 标题\n\n- 项目A\n- 项目B')
    } finally {
      restoreNav()
    }
  })

  it('preserves raw markdown — headings, lists, fenced code, links, bold, italic', async () => {
    const markdown =
      '# 标题\n\n- 项目A\n- 项目B\n\n```python\nprint("test")\n```\n\n**bold** _italic_ [link](https://example.com)'
    const writeText = vi.fn().mockResolvedValue(undefined)
    const restoreNav = installNavigator({ clipboard: { writeText } })
    try {
      await copyMarkdownText(markdown)
      expect(writeText).toHaveBeenCalledWith(markdown)
    } finally {
      restoreNav()
    }
  })

  it('preserves Chinese filenames, line breaks and blockquotes', async () => {
    const markdown = '## 项目说明\n\n> 引用块\n\n第一行\n第二行'
    const writeText = vi.fn().mockResolvedValue(undefined)
    const restoreNav = installNavigator({ clipboard: { writeText } })
    try {
      await copyMarkdownText(markdown)
      expect(writeText).toHaveBeenCalledWith(markdown)
    } finally {
      restoreNav()
    }
  })

  it('returns failed when clipboard API throws AND no document (node)', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('blocked'))
    const restoreNav = installNavigator({ clipboard: { writeText } })
    try {
      // In node env, document is undefined → function returns 'failed'
      const result = await copyMarkdownText('hello')
      expect(result).toBe('failed')
    } finally {
      restoreNav()
    }
  })

  it('returns failed when navigator unavailable and no document', async () => {
    const restoreNav = installNavigator({ clipboard: undefined })
    try {
      const result = await copyMarkdownText('foo')
      expect(result).toBe('failed')
    } finally {
      restoreNav()
    }
  })

  it('returns empty for empty input without touching clipboard', async () => {
    const writeText = vi.fn()
    const restoreNav = installNavigator({ clipboard: { writeText } })
    try {
      expect(await copyMarkdownText('')).toBe('empty')
      expect(await copyMarkdownText(null as unknown as string)).toBe('empty')
      expect(await copyMarkdownText(undefined as unknown as string)).toBe('empty')
      expect(writeText).not.toHaveBeenCalled()
    } finally {
      restoreNav()
    }
  })
})
