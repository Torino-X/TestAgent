import MarkdownIt from 'markdown-it'
import type Renderer from 'markdown-it/lib/renderer.mjs'
import type Token from 'markdown-it/lib/token.mjs'

const CALLOUT_RE = /^:::(tip|warning)\s*\n([\s\S]*?)\n:::\s*$/gm

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

function normalizeCallouts(source: string): string {
  return source.replace(CALLOUT_RE, (_match, kind: string, content: string) => {
    return `\n\`\`\`help-callout-${kind}\n${content.trim()}\n\`\`\`\n`
  })
}

function removeUnsafeImages(source: string): string {
  return source.replace(/!\[([^\]]*)\]\(\s*(?:javascript|data|vbscript):[^\n]*\)/gi, '$1')
}

function plainInline(value: string): string {
  return value
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[`*_~>#|]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function headingAnchor(text: string, seen?: Map<string, number>): string {
  const normalized = plainInline(text).normalize('NFKC').trim().toLocaleLowerCase()
  let base = ''
  let pendingDash = false
  for (const character of normalized) {
    if (/[-_\s]/u.test(character)) {
      pendingDash = base.length > 0
      continue
    }
    if (/^[\p{L}\p{N}]$/u.test(character)) {
      if (pendingDash && base && !base.endsWith('-')) base += '-'
      base += character
      pendingDash = false
    }
  }
  base = base.replace(/-+$/g, '') || 'section'
  if (!seen) return base
  const count = (seen.get(base) ?? 0) + 1
  seen.set(base, count)
  return count === 1 ? base : `${base}-${count}`
}

function safeImageSource(value: string): boolean {
  return value.startsWith('/api/help/assets/') || /^https:\/\//i.test(value)
}

function createMarkdown(): MarkdownIt {
  const md = new MarkdownIt({
    html: false,
    linkify: true,
    typographer: false
  })
  const seen = new Map<string, number>()
  const defaultFence = md.renderer.rules.fence!.bind(md.renderer.rules)
  const defaultLinkOpen = md.renderer.rules.link_open

  md.renderer.rules.heading_open = (tokens: Token[], index: number, options, _env, self: Renderer) => {
    const inline = tokens[index + 1]
    tokens[index].attrSet('id', headingAnchor(inline?.content ?? '', seen))
    return self.renderToken(tokens, index, options)
  }

  md.renderer.rules.fence = (tokens: Token[], index: number, options, env, self: Renderer) => {
    const token = tokens[index]
    const language = token.info.trim().split(/\s+/)[0].toLowerCase()
    if (language === 'mermaid') {
      return `<div class="help-mermaid" data-help-mermaid><pre>${escapeHtml(token.content.trim())}</pre></div>`
    }
    if (language === 'help-callout-tip' || language === 'help-callout-warning') {
      const kind = language.endsWith('warning') ? 'warning' : 'tip'
      const title = kind === 'warning' ? '注意' : '提示'
      return `<aside class="help-callout help-callout--${kind}" role="note"><strong class="help-callout__title">${title}</strong><div class="help-callout__body">${md.render(token.content)}</div></aside>`
    }
    return defaultFence(tokens, index, options, env, self)
  }

  md.renderer.rules.link_open = (tokens: Token[], index: number, options, env, self: Renderer) => {
    const href = tokens[index].attrGet('href') ?? ''
    if (href.startsWith('/help/')) {
      tokens[index].attrSet('data-help-link', 'true')
    } else if (/^https?:\/\//i.test(href)) {
      tokens[index].attrSet('target', '_blank')
      tokens[index].attrSet('rel', 'noopener noreferrer')
    }
    return defaultLinkOpen ? defaultLinkOpen(tokens, index, options, env, self) : self.renderToken(tokens, index, options)
  }

  md.renderer.rules.image = (tokens: Token[], index: number) => {
    const source = tokens[index].attrGet('src') ?? ''
    const alt = tokens[index].content
    if (!safeImageSource(source)) return escapeHtml(alt)
    return `<img src="${escapeHtml(source)}" alt="${escapeHtml(alt)}" loading="lazy">`
  }

  return md
}

export function renderHelpMarkdown(source: string): string {
  return createMarkdown().render(normalizeCallouts(removeUnsafeImages(source)))
}
