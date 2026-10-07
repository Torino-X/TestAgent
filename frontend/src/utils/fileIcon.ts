/**
 * 文件类型图标解析工具 — 统一映射 Word/Excel/Markdown/PDF 文件图标。
 *
 * Phase 2.9A.x: 接入 frontend/src/assets/file-svg/ 中已准备的 SVG 资源,
 * 取代 FileAttachmentCard / ArtifactDownloadCard 中写死的 DocumentTextOutline。
 *
 * 解析优先级:
 *   1. 文件扩展名(忽略大小写、剥离 URL 查询/hash、兼容中文路径)
 *   2. MIME 类型(扩展名缺失或 application/octet-stream 兜底)
 *   3. 返回 generic 表示无法识别 — 调用方应继续使用通用图标
 *
 * 设计约束:
 *   - 静态 import,Vite 构建阶段即可发现缺失资源,不会运行时 404
 *   - 文件名严格按 Linux 大小写敏感处理 (MD.svg 不是 md.svg)
 *   - 不抛异常;无效输入返回 generic
 */

import wordIcon from '@/assets/file-svg/word.svg'
import excelIcon from '@/assets/file-svg/excel.svg'
import markdownIcon from '@/assets/file-svg/MD.svg'
import pdfIcon from '@/assets/file-svg/pdf.svg'
import colorWordIcon from '@/assets/file-svg-color/word.svg'
import colorExcelIcon from '@/assets/file-svg-color/excel.svg'
import colorMarkdownIcon from '@/assets/file-svg-color/md.svg'
import colorPdfIcon from '@/assets/file-svg-color/pdf.svg'
import colorTextIcon from '@/assets/file-svg-color/txt.svg'
import colorImageIcon from '@/assets/file-svg-color/image.svg'

export type SupportedFileIconKind = 'word' | 'excel' | 'markdown' | 'pdf' | 'generic'

export interface ResolvedFileIcon {
  kind: SupportedFileIconKind
  src?: string
}

export type ColoredFileIconKind = SupportedFileIconKind | 'text' | 'image'

export interface ResolvedColoredFileIcon {
  kind: ColoredFileIconKind
  src?: string
}

const WORD_EXTENSIONS = new Set(['doc', 'docx'])
const EXCEL_EXTENSIONS = new Set(['xls', 'xlsx', 'xlsm'])
const MARKDOWN_EXTENSIONS = new Set(['md', 'markdown'])
const PDF_EXTENSIONS = new Set(['pdf'])
const TEXT_EXTENSIONS = new Set(['txt', 'text', 'log', 'csv'])
const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'heic', 'jpeg', 'jpg', 'png', 'svg', 'tif', 'tiff', 'webp'])

const WORD_MIME_PREFIXES = ['application/msword']
const WORD_MIME_EXACT = new Set([
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
])
const EXCEL_MIME_EXACT = new Set([
  'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel.sheet.macroenabled.12'
])
const MARKDOWN_MIME_EXACT = new Set(['text/markdown', 'text/x-markdown'])
const PDF_MIME_EXACT = new Set(['application/pdf'])

export function getNormalizedExtension(filename?: string | null): string {
  if (!filename) return ''
  const stripped = filename.trim().split('?')[0].split('#')[0]
  const lastDot = stripped.lastIndexOf('.')
  if (lastDot < 0) return ''
  return stripped.slice(lastDot + 1).toLowerCase()
}

function normalizeMime(mimeType?: string | null): string {
  if (!mimeType) return ''
  return mimeType.trim().split(';')[0].toLowerCase()
}

function matchByExtension(ext: string): SupportedFileIconKind {
  if (WORD_EXTENSIONS.has(ext)) return 'word'
  if (EXCEL_EXTENSIONS.has(ext)) return 'excel'
  if (MARKDOWN_EXTENSIONS.has(ext)) return 'markdown'
  if (PDF_EXTENSIONS.has(ext)) return 'pdf'
  return 'generic'
}

function matchByMime(mime: string): SupportedFileIconKind {
  if (!mime) return 'generic'
  if (WORD_MIME_EXACT.has(mime)) return 'word'
  if (EXCEL_MIME_EXACT.has(mime)) return 'excel'
  if (MARKDOWN_MIME_EXACT.has(mime)) return 'markdown'
  if (PDF_MIME_EXACT.has(mime)) return 'pdf'
  if (WORD_MIME_PREFIXES.some((p) => mime.startsWith(p))) return 'word'
  return 'generic'
}

export function resolveFileIconKind(
  filename?: string | null,
  mimeType?: string | null
): SupportedFileIconKind {
  const ext = getNormalizedExtension(filename)
  if (ext) {
    const byExt = matchByExtension(ext)
    if (byExt !== 'generic') return byExt
  }
  const mime = normalizeMime(mimeType)
  if (mime) return matchByMime(mime)
  return 'generic'
}

export function resolveFileIcon(
  filename?: string | null,
  mimeType?: string | null
): ResolvedFileIcon {
  const kind = resolveFileIconKind(filename, mimeType)
  switch (kind) {
    case 'word':
      return { kind, src: wordIcon }
    case 'excel':
      return { kind, src: excelIcon }
    case 'markdown':
      return { kind, src: markdownIcon }
    case 'pdf':
      return { kind, src: pdfIcon }
    default:
      return { kind: 'generic' }
  }
}

/**
 * Resolve the colored assets placed in ``assets/file-svg-color``.
 * The Library uses this variant while retaining its existing icon square.
 */
export function resolveColoredFileIcon(
  filename?: string | null,
  mimeType?: string | null
): ResolvedColoredFileIcon {
  const kind = resolveFileIconKind(filename, mimeType)
  switch (kind) {
    case 'word':
      return { kind, src: colorWordIcon }
    case 'excel':
      return { kind, src: colorExcelIcon }
    case 'markdown':
      return { kind, src: colorMarkdownIcon }
    case 'pdf':
      return { kind, src: colorPdfIcon }
  }

  const extension = getNormalizedExtension(filename)
  const mime = normalizeMime(mimeType)
  if (IMAGE_EXTENSIONS.has(extension) || mime.startsWith('image/')) {
    return { kind: 'image', src: colorImageIcon }
  }
  if (TEXT_EXTENSIONS.has(extension) || mime.startsWith('text/')) {
    return { kind: 'text', src: colorTextIcon }
  }
  return { kind: 'generic' }
}

export function isAssetIcon(icon: ResolvedFileIcon): icon is ResolvedFileIcon & { src: string } {
  return Boolean(icon.src)
}
