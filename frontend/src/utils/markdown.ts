const SAFE_URL_PROTOCOLS = ['http:', 'https:', 'mailto:']

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function isSafeUrl(value: string): boolean {
  try {
    const url = new URL(value, 'https://testagent.local')
    if (url.origin === 'https://testagent.local' && value.trim().startsWith('/')) return true
    return SAFE_URL_PROTOCOLS.includes(url.protocol)
  } catch {
    return false
  }
}

function renderInline(value: string): string {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label: string, href: string) => {
      const normalizedHref = href.replace(/&amp;/g, '&').trim()
      if (!isSafeUrl(normalizedHref)) return label
      return `<a href="${escapeHtml(normalizedHref)}" target="_blank" rel="noopener noreferrer">${label}</a>`
    })
}

function isBlank(line: string): boolean {
  return line.trim().length === 0
}

function isFence(line: string): boolean {
  return line.trim().startsWith('```')
}

const HEADING_PATTERN = /^(#{1,6})\s+(\S.*)$/

function parseHeading(line: string): { marks: string; content: string } | null {
  const match = line.match(HEADING_PATTERN)
  if (!match) return null
  return { marks: match[1], content: match[2] }
}

function isHeading(line: string): boolean {
  return parseHeading(line) !== null
}

function isQuote(line: string): boolean {
  return /^>\s?/.test(line)
}

function isUnorderedList(line: string): boolean {
  return /^\s*[-*+]\s+/.test(line)
}

function isOrderedList(line: string): boolean {
  return /^\s*\d+\.\s+/.test(line)
}

function isTableSeparator(line: string): boolean {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line)
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

/**
 * Phase 2.9A.X: 长段 narrative 自动分段。
 *
 * LLM 输出 NARRATIVE 字段时经常写成单段连续文字,不带 `\n\n` 段落分隔,
 * 导致 ``renderMarkdown`` 渲染成单个 ``<p>`` — 视觉上就是"一坨文字"。
 *
 * 这里做兜底预处理:当文本长度 >= 阈值、且不含 `\n\n` 时,按中文句末标点
 * (。！？) + 后跟汉字 / 英文句末标点 (.!?) + 后跟大写字母开头新句的
 * 模式插入 `\n\n`。数字开头("3.5")等小数点不被误切。
 *
 * 已有 `\n\n` 的输入完全尊重 LLM 意图,不二次切分。
 */
function preprocessLongParagraphs(source: string): string {
  if (!source) return source
  if (source.includes('\n\n')) return source
  if (source.length < 240) return source

  // 中文叙事:句末标点(。！？)+ 可选闭引号 + 后跟汉字 → 切
  let result = source.replace(
    /([。！？]+)(["'"”’)\]】」』]?)(?=[一-鿿])/g,
    '$1$2\n\n',
  )
  // 英文叙事兜底:句末标点(.!?) + 后跟大写字母开头新句 → 切
  if (result === source) {
    result = source.replace(
      /([.!?]+)(["'’)\]}>]?)\s+(?=[A-Z])/g,
      '$1$2\n\n',
    )
  }
  // 合并多余空行(保险)
  return result.replace(/\n{3,}/g, '\n\n').trim()
}

function isBlockStart(line: string, nextLine?: string): boolean {
  return (
    isBlank(line) ||
    isFence(line) ||
    isHeading(line) ||
    isQuote(line) ||
    isUnorderedList(line) ||
    isOrderedList(line) ||
    (!!nextLine && line.includes('|') && isTableSeparator(nextLine))
  )
}

function renderCodeBlock(language: string, code: string): string {
  const languageLabel = language || '代码'
  const className = language ? ` class="language-${escapeHtml(language)}"` : ''

  return [
    '<figure class="markdown-code-block">',
    '<figcaption class="markdown-code-block__header">',
    `<span class="markdown-code-block__language">${escapeHtml(languageLabel)}</span>`,
    '<button type="button" class="markdown-code-block__copy" data-code-copy aria-label="复制代码" title="复制代码">',
    '<span class="markdown-code-block__copy-icon" aria-hidden="true"></span>',
    '<span class="markdown-code-block__copied-icon" aria-hidden="true">✓</span>',
    '</button>',
    '</figcaption>',
    `<pre><code${className}>${escapeHtml(code)}</code></pre>`,
    '</figure>',
  ].join('')
}

export function renderMarkdown(source: string): string {
  // Phase 2.9A.X: 长段 narrative 自动分段预处理(LLM 不带 \n\n 兜底)
  const normalized = preprocessLongParagraphs(source)
  const lines = normalized.replace(/\r\n/g, '\n').split('\n')
  const html: string[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]

    if (isBlank(line)) {
      index += 1
      continue
    }

    if (isFence(line)) {
      const language = line.trim().slice(3).trim()
      const codeLines: string[] = []
      index += 1
      while (index < lines.length && !isFence(lines[index])) {
        codeLines.push(lines[index])
        index += 1
      }
      if (index < lines.length) index += 1
      html.push(renderCodeBlock(language, codeLines.join('\n')))
      continue
    }

    const heading = parseHeading(line)
    if (heading) {
      const level = heading.marks.length
      html.push(`<h${level}>${renderInline(heading.content)}</h${level}>`)
      index += 1
      continue
    }

    if (isQuote(line)) {
      const quoteLines: string[] = []
      while (index < lines.length && isQuote(lines[index])) {
        quoteLines.push(lines[index].replace(/^>\s?/, ''))
        index += 1
      }
      html.push(`<blockquote>${renderInline(quoteLines.join(' '))}</blockquote>`)
      continue
    }

    if (isUnorderedList(line) || isOrderedList(line)) {
      const ordered = isOrderedList(line)
      const tag = ordered ? 'ol' : 'ul'
      const items: string[] = []
      while (index < lines.length && (ordered ? isOrderedList(lines[index]) : isUnorderedList(lines[index]))) {
        items.push(lines[index].replace(ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/, ''))
        index += 1
      }
      html.push(`<${tag}>${items.map((item) => `<li>${renderInline(item)}</li>`).join('')}</${tag}>`)
      continue
    }

    if (line.includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const headers = splitTableRow(line)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && lines[index].includes('|') && !isBlank(lines[index])) {
        rows.push(splitTableRow(lines[index]))
        index += 1
      }
      html.push(
        `<table><thead><tr>${headers.map((cell) => `<th>${renderInline(cell)}</th>`).join('')}</tr></thead><tbody>${rows
          .map((row) => `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join('')}</tr>`)
          .join('')}</tbody></table>`
      )
      continue
    }

    const paragraphLines: string[] = []
    while (index < lines.length && !isBlockStart(lines[index], lines[index + 1])) {
      paragraphLines.push(lines[index])
      index += 1
    }
    html.push(`<p>${renderInline(paragraphLines.join(' '))}</p>`)
  }

  return html.join('\n')
}
