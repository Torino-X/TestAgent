import { describe, expect, it } from 'vitest'
import {
  getNormalizedExtension,
  isAssetIcon,
  resolveColoredFileIcon,
  resolveFileIcon,
  resolveFileIconKind
} from '@/utils/fileIcon'

describe('fileIcon utility', () => {
  describe('getNormalizedExtension', () => {
    it('returns extension lowercased and stripped of dot', () => {
      expect(getNormalizedExtension('report.DOCX')).toBe('docx')
    })

    it('strips URL query and hash', () => {
      expect(getNormalizedExtension('report.pdf?version=1&token=abc#page')).toBe('pdf')
    })

    it('returns empty string when no extension', () => {
      expect(getNormalizedExtension('README')).toBe('')
    })

    it('returns empty string for null or undefined', () => {
      expect(getNormalizedExtension(null)).toBe('')
      expect(getNormalizedExtension(undefined)).toBe('')
      expect(getNormalizedExtension('')).toBe('')
    })

    it('handles chinese filenames', () => {
      expect(getNormalizedExtension('报告.DOCX')).toBe('docx')
      expect(getNormalizedExtension('缺陷统计.XLSM')).toBe('xlsm')
      expect(getNormalizedExtension('说明文档.MD')).toBe('md')
    })

    it('treats dotfiles correctly', () => {
      expect(getNormalizedExtension('.hidden')).toBe('hidden')
    })

    it('trims whitespace', () => {
      expect(getNormalizedExtension('  report.docx  ')).toBe('docx')
    })

    it('handles last-dot when path contains earlier dots', () => {
      expect(getNormalizedExtension('archive.tar.gz')).toBe('gz')
    })
  })

  describe('resolveFileIconKind by extension', () => {
    it.each([
      ['doc', 'word'],
      ['docx', 'word'],
      ['xls', 'excel'],
      ['xlsx', 'excel'],
      ['xlsm', 'excel'],
      ['md', 'markdown'],
      ['pdf', 'pdf']
    ])('.%s maps to %s', (ext, kind) => {
      expect(resolveFileIconKind(`file.${ext}`)).toBe(kind)
    })

    it('uppercase .DOCX → word', () => {
      expect(resolveFileIconKind('FILE.DOCX')).toBe('word')
    })

    it('uppercase .MD → markdown', () => {
      expect(resolveFileIconKind('README.MD')).toBe('markdown')
    })

    it('unknown extension → generic', () => {
      expect(resolveFileIconKind('unknown.txt')).toBe('generic')
    })
  })

  describe('resolveFileIconKind by mime', () => {
    it('application/msword → word', () => {
      expect(resolveFileIconKind(null, 'application/msword')).toBe('word')
    })

    it('application/vnd.openxmlformats-officedocument.wordprocessingml.document → word', () => {
      expect(
        resolveFileIconKind(null, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
      ).toBe('word')
    })

    it('application/vnd.ms-excel → excel', () => {
      expect(resolveFileIconKind(null, 'application/vnd.ms-excel')).toBe('excel')
    })

    it('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet → excel', () => {
      expect(
        resolveFileIconKind(null, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
      ).toBe('excel')
    })

    it('application/vnd.ms-excel.sheet.macroenabled.12 → excel', () => {
      expect(resolveFileIconKind(null, 'application/vnd.ms-excel.sheet.macroenabled.12')).toBe('excel')
    })

    it('text/markdown → markdown', () => {
      expect(resolveFileIconKind(null, 'text/markdown')).toBe('markdown')
    })

    it('text/x-markdown → markdown', () => {
      expect(resolveFileIconKind(null, 'text/x-markdown')).toBe('markdown')
    })

    it('application/pdf → pdf', () => {
      expect(resolveFileIconKind(null, 'application/pdf')).toBe('pdf')
    })

    it('application/octet-stream → generic', () => {
      expect(resolveFileIconKind(null, 'application/octet-stream')).toBe('generic')
    })

    it('mime is case-insensitive', () => {
      expect(resolveFileIconKind(null, 'APPLICATION/PDF')).toBe('pdf')
    })

    it('mime with charset parameter is parsed correctly', () => {
      expect(resolveFileIconKind(null, 'application/pdf; charset=utf-8')).toBe('pdf')
    })
  })

  describe('priority: extension > mime', () => {
    it('extension wins when both provided', () => {
      expect(resolveFileIconKind('test.xlsx', 'application/pdf')).toBe('excel')
    })

    it('falls back to mime when extension is unknown', () => {
      expect(resolveFileIconKind('mystery.zzz', 'application/pdf')).toBe('pdf')
    })

    it('falls back to mime when extension is missing', () => {
      expect(resolveFileIconKind('noextfile', 'application/pdf')).toBe('pdf')
    })
  })

  describe('resolveFileIcon asset url', () => {
    it('returns generic for unknown types', () => {
      const icon = resolveFileIcon('mystery.zzz')
      expect(icon.kind).toBe('generic')
      expect(icon.src).toBeUndefined()
    })

    it('returns asset url for word', () => {
      const icon = resolveFileIcon('test.docx')
      expect(icon.kind).toBe('word')
      expect(icon.src).toMatch(/word\.svg$/)
      expect(isAssetIcon(icon)).toBe(true)
    })

    it('returns asset url for excel', () => {
      const icon = resolveFileIcon('test.xlsx')
      expect(icon.kind).toBe('excel')
      expect(icon.src).toMatch(/excel\.svg$/)
    })

    it('returns asset url for markdown', () => {
      const icon = resolveFileIcon('README.md')
      expect(icon.kind).toBe('markdown')
      expect(icon.src).toMatch(/MD\.svg$/)
    })

    it('returns asset url for pdf', () => {
      const icon = resolveFileIcon('test.pdf')
      expect(icon.kind).toBe('pdf')
      expect(icon.src).toMatch(/pdf\.svg$/)
    })

    it('isAssetIcon narrows generic to false', () => {
      expect(isAssetIcon(resolveFileIcon('a.zip'))).toBe(false)
    })
  })

  describe('resolveColoredFileIcon', () => {
    it.each([
      ['方案.docx', undefined, 'word', 'file-svg-color/word.svg'],
      ['数据.xlsx', undefined, 'excel', 'file-svg-color/excel.svg'],
      ['说明.md', undefined, 'markdown', 'file-svg-color/md.svg'],
      ['预览.pdf', undefined, 'pdf', 'file-svg-color/pdf.svg'],
      ['日志.txt', undefined, 'text', 'file-svg-color/txt.svg'],
      ['封面.webp', undefined, 'image', 'file-svg-color/image.svg']
    ])('uses the colored %s icon from the dedicated asset directory', (filename, mime, kind, asset) => {
      const icon = resolveColoredFileIcon(filename, mime)
      expect(icon.kind).toBe(kind)
      expect(icon.src).toMatch(new RegExp(`${asset.replace('.', '\\.')}\\?*$`))
    })

    it('uses image MIME type when the filename has no extension', () => {
      expect(resolveColoredFileIcon('upload', 'image/png').kind).toBe('image')
    })
  })
})
