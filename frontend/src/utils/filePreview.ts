import type { FileAttachment } from '@/types'

export function canPreviewFileAttachment(file: FileAttachment): boolean {
  return Boolean(file.id) && !file.localOnly && file.status !== 'uploading' && file.status !== 'failed'
}

export function buildChatFilePreviewLocation(
  file: FileAttachment,
  conversationId: string,
  conversationTitle: string
) {
  return {
    name: 'document-preview',
    params: { itemId: file.id },
    query: {
      from: 'chat',
      conversationId,
      conversationTitle
    }
  }
}
