import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/fileApi', () => ({
  fetchConversationFiles: vi.fn(),
  uploadFile: vi.fn(),
  confirmFileType: vi.fn(),
  deleteFile: vi.fn()
}))

import * as fileApi from '@/api/fileApi'
import type { FileAttachment } from '@/types'
import { useFileStore } from './fileStore'

function uploadedFile(id = 'file_a'): FileAttachment {
  return {
    id,
    name: 'requirements.pdf',
    type: 'requirement_doc',
    size: '1 KB',
    extension: '.pdf',
    status: 'uploaded'
  }
}

function selectedFile() {
  return { name: 'requirements.pdf', type: 'application/pdf', size: 5 } as File
}

describe('fileStore upload state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('stores uploaded files immediately without waiting for context indexing', async () => {
    const file = selectedFile()
    vi.mocked(fileApi.uploadFile).mockResolvedValueOnce(uploadedFile())

    const store = useFileStore()
    const result = await store.uploadFile(file, 'conv_a')

    expect(result.id).toBe('file_a')
    expect(store.files).toEqual([uploadedFile()])
    expect(store.uploading).toBe(false)
    expect(fileApi.uploadFile).toHaveBeenCalledWith(file, 'conv_a', undefined, undefined)
  })

  it('fetches conversation files without starting frontend index polling', async () => {
    vi.mocked(fileApi.fetchConversationFiles).mockResolvedValueOnce([uploadedFile('file_b')])

    const store = useFileStore()
    await store.fetchConversationFiles('conv_a')

    expect(store.files).toEqual([uploadedFile('file_b')])
    expect('refreshIndexStatus' in store).toBe(false)
    expect('pendingIndexSummary' in store).toBe(false)
    expect('hasPendingIndex' in store).toBe(false)
  })

  it('removes deleted files without tracking derived index state', async () => {
    vi.mocked(fileApi.uploadFile).mockResolvedValueOnce(uploadedFile())
    vi.mocked(fileApi.deleteFile).mockResolvedValueOnce(undefined)

    const store = useFileStore()
    await store.uploadFile(selectedFile(), 'conv_a')
    await store.deleteFile('file_a')

    expect(store.files).toEqual([])
    expect(fileApi.deleteFile).toHaveBeenCalledWith('file_a')
  })
})
