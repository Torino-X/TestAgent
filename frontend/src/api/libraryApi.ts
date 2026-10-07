import { apiDelete, apiDownloadBlob, apiGet, apiPatch, apiPost, apiUpload } from './request'
import type {
  LibraryCategory, LibraryFileType, LibraryItem, LibraryItemList, LibraryScope, LibrarySourceFilter
} from '@/types/library'

interface ApiLibraryItem {
  id: string
  name: string
  kind: LibraryItem['kind']
  source: LibraryItem['source']
  mime_type?: string | null
  extension?: string | null
  size_bytes?: number | null
  modified_at: string
  conversation_id?: string | null
  artifact_type?: string | null
  thumbnail_url?: string | null
  download_url?: string | null
  deleted_at?: string | null
}

interface ApiLibraryItemList {
  items: ApiLibraryItem[]
  total: number
  page?: number
  page_size?: number
  has_more?: boolean
}

function mapLibraryItem(item: ApiLibraryItem): LibraryItem {
  return {
    id: item.id,
    name: item.name,
    kind: item.kind,
    source: item.source,
    mimeType: item.mime_type,
    extension: item.extension ?? '',
    sizeBytes: item.size_bytes,
    modifiedAt: item.modified_at,
    conversationId: item.conversation_id,
    artifactType: item.artifact_type,
    thumbnailUrl: item.thumbnail_url,
    downloadUrl: item.download_url,
    deletedAt: item.deleted_at
  }
}

export async function fetchLibraryItems(
  category: LibraryCategory = 'all',
  query = '',
  options: {
    scope?: LibraryScope
    source?: LibrarySourceFilter
    fileType?: LibraryFileType
    page?: number
    pageSize?: number
  } = {}
): Promise<LibraryItemList> {
  const params = new URLSearchParams({
    category,
    scope: options.scope ?? 'active',
    source: options.source ?? 'all',
    file_type: options.fileType ?? 'all',
    page: String(options.page ?? 1),
    page_size: String(options.pageSize ?? 50)
  })
  if (query.trim()) params.set('q', query.trim())
  const payload = await apiGet<ApiLibraryItemList>(`/api/library/items?${params.toString()}`)
  const page = payload.page ?? options.page ?? 1
  const pageSize = payload.page_size ?? options.pageSize ?? 50
  return {
    items: payload.items.map(mapLibraryItem),
    total: payload.total,
    page,
    pageSize,
    hasMore: payload.has_more ?? page * pageSize < payload.total
  }
}

/** Upload files directly to the user's library without creating a chat. */
export async function uploadLibraryFiles(files: File[]): Promise<LibraryItem[]> {
  return Promise.all(files.map(async (file) => {
    const formData = new FormData()
    formData.append('file', file)
    const payload = await apiUpload<ApiLibraryItem>('/api/library/upload', formData)
    return mapLibraryItem(payload)
  }))
}

export async function fetchLibraryItem(itemId: string): Promise<LibraryItem> {
  const payload = await apiGet<ApiLibraryItem>(`/api/library/items/${encodeURIComponent(itemId)}`)
  return mapLibraryItem(payload)
}

export async function renameLibraryItem(itemId: string, name: string): Promise<LibraryItem> {
  const payload = await apiPatch<ApiLibraryItem>(`/api/library/items/${encodeURIComponent(itemId)}`, { name })
  return mapLibraryItem(payload)
}

export async function deleteLibraryItem(itemId: string): Promise<void> {
  await apiDelete(`/api/library/items/${encodeURIComponent(itemId)}`)
}

export async function restoreLibraryItem(itemId: string): Promise<LibraryItem> {
  const payload = await apiPost<ApiLibraryItem>(`/api/library/items/${encodeURIComponent(itemId)}/restore`, {})
  return mapLibraryItem(payload)
}

export async function permanentlyDeleteLibraryItem(itemId: string): Promise<void> {
  await apiDelete(`/api/library/items/${encodeURIComponent(itemId)}/permanent`)
}

export async function downloadLibraryItem(item: LibraryItem): Promise<void> {
  const { blob, filename: serverFilename } = await fetchLibraryItemBlob(item)
  const objectUrl = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = serverFilename || item.name
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
  }
}

/** Return an authenticated file Blob so it can be attached to a new chat. */
export async function fetchLibraryItemBlob(item: LibraryItem) {
  if (!item.downloadUrl) throw new Error('该文件暂时无法下载')
  return apiDownloadBlob(item.downloadUrl)
}
