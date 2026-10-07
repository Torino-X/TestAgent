import { describe, expect, it } from 'vitest'
import type { FileAttachment } from '@/types'
import { buildChatFilePreviewLocation, canPreviewFileAttachment } from './filePreview'

function attachment(overrides: Partial<FileAttachment> = {}): FileAttachment {
  return {
    id: 'file_123',
    name: '需求说明书.docx',
    size: '42 KB',
    extension: '.docx',
    type: 'requirement_doc',
    status: 'uploaded',
    ...overrides
  }
}

describe('conversation attachment preview contract', () => {
  it('only enables previews after a server-backed upload succeeds', () => {
    expect(canPreviewFileAttachment(attachment())).toBe(true)
    expect(canPreviewFileAttachment(attachment({ status: 'confirmed' }))).toBe(true)
    expect(canPreviewFileAttachment(attachment({ id: '' }))).toBe(false)
    expect(canPreviewFileAttachment(attachment({ localOnly: true }))).toBe(false)
    expect(canPreviewFileAttachment(attachment({ status: 'uploading' }))).toBe(false)
    expect(canPreviewFileAttachment(attachment({ status: 'failed' }))).toBe(false)
  })

  it('builds a chat-aware preview location for the corresponding uploaded file', () => {
    expect(buildChatFilePreviewLocation(attachment(), 'conv_456', '智慧校园测试')).toEqual({
      name: 'document-preview',
      params: { itemId: 'file_123' },
      query: {
        from: 'chat',
        conversationId: 'conv_456',
        conversationTitle: '智慧校园测试'
      }
    })
  })
})
