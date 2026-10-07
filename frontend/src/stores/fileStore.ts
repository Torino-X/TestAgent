/**
 * File store — uploaded files per conversation.
 *
 * The chat UI treats a successfully uploaded file as sendable immediately.
 * Context Engine indexing is a backend concern and must not block the input.
 */

import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { FileAttachment, FileType } from '@/types'
import * as fileApi from '@/api/fileApi'

export const useFileStore = defineStore('file', () => {
  const files = ref<FileAttachment[]>([])
  const uploading = ref(false)
  const activeUploadCount = ref(0)

  async function fetchConversationFiles(conversationId: string) {
    files.value = await fileApi.fetchConversationFiles(conversationId)
  }

  async function uploadFile(
    file: File,
    conversationId?: string,
    fileType?: FileType,
    options?: fileApi.UploadFileOptions
  ): Promise<FileAttachment> {
    activeUploadCount.value += 1
    uploading.value = true
    try {
      const uploaded = await fileApi.uploadFile(file, conversationId, fileType, options)
      files.value = [...files.value, uploaded]
      return uploaded
    } finally {
      activeUploadCount.value = Math.max(0, activeUploadCount.value - 1)
      uploading.value = activeUploadCount.value > 0
    }
  }

  async function confirmFileType(fileId: string, fileType: FileType) {
    const updated = await fileApi.confirmFileType(fileId, fileType)
    const idx = files.value.findIndex((f) => f.id === fileId)
    const existing = idx >= 0 ? files.value[idx] : undefined
    const merged = existing
      ? {
          ...existing,
          ...updated,
          name: updated.name === fileId ? existing.name : updated.name,
          size: updated.size || existing.size,
          extension: updated.extension === `.${fileId}` ? existing.extension : updated.extension
        }
      : updated
    if (idx >= 0) files.value[idx] = merged
    return merged
  }

  async function deleteFile(fileId: string) {
    await fileApi.deleteFile(fileId)
    files.value = files.value.filter((f) => f.id !== fileId)
  }

  function clearFiles() {
    files.value = []
  }

  return {
    files,
    uploading,
    fetchConversationFiles,
    uploadFile,
    confirmFileType,
    deleteFile,
    clearFiles
  }
})
