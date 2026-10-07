import { describe, expect, it, vi } from 'vitest'
import { extractClipboardFiles } from '@/utils/clipboardFiles'
import { extractDroppedFiles, hasFileTransfer, isDirectoryTransfer } from '@/utils/dragDropFiles'
import { resetTextareaHeight, resizeTextareaToContent } from '@/utils/textareaAutosize'

interface ClipboardItemStub {
  kind: string
  getAsFile: () => File | null
}

interface ClipboardDataStub {
  files?: File[]
  items?: ClipboardItemStub[]
}

interface DataTransferItemStub {
  kind: string
  getAsFile: () => File | null
  webkitGetAsEntry?: () => { isDirectory?: boolean } | null
}

interface DataTransferStub {
  types?: string[]
  files?: File[]
  items?: DataTransferItemStub[]
  dropEffect?: string
}

function makeFile(name: string, options: { size?: number; type?: string; lastModified?: number } = {}) {
  return {
    name,
    size: options.size ?? 128,
    type: options.type ?? 'application/octet-stream',
    lastModified: options.lastModified ?? 1
  } as File
}

function clipboardEvent(data: ClipboardDataStub): ClipboardEvent {
  return { clipboardData: data } as unknown as ClipboardEvent
}

function dataTransfer(data: DataTransferStub): DataTransfer {
  return data as unknown as DataTransfer
}

describe('chat file import helpers', () => {
  it('extracts clipboard files from files and items while deduping the same batch', () => {
    const doc = makeFile('requirement.docx', { size: 2048, type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
    const template = makeFile('template.docx', { size: 1024 })

    const files = extractClipboardFiles(
      clipboardEvent({
        files: [doc],
        items: [
          { kind: 'string', getAsFile: () => template },
          { kind: 'file', getAsFile: () => doc },
          { kind: 'file', getAsFile: () => template },
          { kind: 'file', getAsFile: () => null }
        ]
      })
    )

    expect(files).toEqual([doc, template])
  })

  it('leaves text-only clipboard events untouched for browser default paste', () => {
    const preventDefault = vi.fn()
    const event = Object.assign(
      clipboardEvent({
        files: [],
        items: [{ kind: 'string', getAsFile: () => null }]
      }),
      { preventDefault }
    )

    expect(extractClipboardFiles(event)).toEqual([])
    expect(preventDefault).not.toHaveBeenCalled()
  })

  it('detects file drags without treating text drags as files', () => {
    expect(hasFileTransfer(dataTransfer({ types: ['Files'] }))).toBe(true)
    expect(hasFileTransfer(dataTransfer({ items: [{ kind: 'file', getAsFile: () => makeFile('a.docx') }] }))).toBe(true)
    expect(hasFileTransfer(dataTransfer({ types: ['text/plain'], items: [{ kind: 'string', getAsFile: () => null }] }))).toBe(false)
    expect(hasFileTransfer(null)).toBe(false)
  })

  it('extracts dropped files once and preserves order', () => {
    const first = makeFile('first.docx', { size: 1, lastModified: 1 })
    const second = makeFile('second.docx', { size: 2, lastModified: 2 })

    const files = extractDroppedFiles(
      dataTransfer({
        types: ['Files'],
        files: [first],
        items: [
          { kind: 'file', getAsFile: () => first },
          { kind: 'file', getAsFile: () => second }
        ]
      })
    )

    expect(files).toEqual([first, second])
  })

  it('rejects directory transfers without returning empty pseudo files', () => {
    const transfer = dataTransfer({
      types: ['Files'],
      items: [
        {
          kind: 'file',
          getAsFile: () => makeFile('folder'),
          webkitGetAsEntry: () => ({ isDirectory: true })
        }
      ]
    })

    expect(isDirectoryTransfer(transfer)).toBe(true)
    expect(extractDroppedFiles(transfer)).toEqual([])
  })

  it('resizes textarea height within the configured min and max bounds', () => {
    const textarea = {
      scrollHeight: 120,
      style: { height: '', overflowY: '' }
    } as unknown as HTMLTextAreaElement

    resizeTextareaToContent(textarea, { minHeight: 56, maxHeight: 180 })
    expect(textarea.style.height).toBe('120px')
    expect(textarea.style.overflowY).toBe('hidden')
  })

  it('uses internal vertical scroll after textarea reaches max height', () => {
    const textarea = {
      scrollHeight: 260,
      style: { height: '', overflowY: '' }
    } as unknown as HTMLTextAreaElement

    resizeTextareaToContent(textarea, { minHeight: 56, maxHeight: 180 })
    expect(textarea.style.height).toBe('180px')
    expect(textarea.style.overflowY).toBe('auto')
  })

  it('restores textarea to the existing minimum height after clear or send', () => {
    const textarea = {
      scrollHeight: 260,
      style: { height: '180px', overflowY: 'auto' }
    } as unknown as HTMLTextAreaElement

    resetTextareaHeight(textarea, { minHeight: 56, maxHeight: 180 })
    expect(textarea.style.height).toBe('56px')
    expect(textarea.style.overflowY).toBe('hidden')
  })
})
