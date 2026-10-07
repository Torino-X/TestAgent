import type { FileAttachment, FileType, FileUploadStatus } from '@/types'
import { apiDelete, apiGet, apiPost, apiUpload } from './request'

export interface ApiFile {
  id?: string
  file_id?: string
  file_name?: string
  original_name?: string
  file_size?: number
  file_ext?: string
  file_type?: FileType
  status?: string
  upload_status?: string
  description?: string
}

interface ApiFileList {
  items: ApiFile[]
}

export interface UploadFileOptions {
  onUploadProgress?: (percent: number) => void
}

function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

export function mapFile(file: ApiFile): FileAttachment {
  const name = file.file_name ?? file.original_name ?? file.file_id ?? file.id ?? 'file'
  return {
    id: file.file_id ?? file.id ?? '',
    name,
    size: file.file_size ? formatFileSize(file.file_size) : '',
    extension: file.file_ext ?? (name.includes('.') ? `.${name.split('.').pop() ?? ''}` : ''),
    type: file.file_type ?? 'unknown',
    status: ((file.upload_status ?? file.status) as FileUploadStatus | undefined) ?? 'uploaded',
    description: file.description
  }
}

function normalizeFileList(files: ApiFile[] | ApiFileList): ApiFile[] {
  return Array.isArray(files) ? files : files.items
}

export async function uploadFile(
  file: File,
  conversationId?: string,
  fileType?: FileType,
  options?: UploadFileOptions
): Promise<FileAttachment> {
  const formData = new FormData()
  formData.append('file', file)
  if (conversationId) formData.append('conversation_id', conversationId)
  if (fileType) formData.append('file_type', fileType)

  const uploaded = await apiUpload<ApiFile>('/api/files/upload', formData, {
    onUploadProgress: options?.onUploadProgress
  })
  return mapFile(uploaded)
}

export async function fetchConversationFiles(conversationId: string): Promise<FileAttachment[]> {
  const files = await apiGet<ApiFile[] | ApiFileList | { files: ApiFile[]; total: number }>(`/api/conversations/${conversationId}/files`)
  if ('files' in files) return files.files.map(mapFile)
  return normalizeFileList(files).map(mapFile)
}

export async function confirmFileType(fileId: string, fileType: FileType): Promise<FileAttachment> {
  const updated = await apiPost<ApiFile>(
    `/api/files/${fileId}/confirm-type`,
    { file_type: fileType }
  )
  return mapFile(updated)
}

export async function deleteFile(fileId: string): Promise<void> {
  await apiDelete(`/api/files/${fileId}`)
}
