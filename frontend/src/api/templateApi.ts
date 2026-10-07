import { apiDelete, apiDownloadBlob, apiGet, apiPost, apiUpload } from './request'
import { mapConversation } from './conversationApi'
import { mapFile, type ApiFile } from './fileApi'
import { fetchDocumentPreviewFromUrl } from './documentPreviewApi'
import type { DocumentPreview } from '@/types/library'
import type {
  TemplateMarketItem,
  TemplatePage,
  TemplateUploadPayload,
  TemplateUseResult,
  UserTemplateItem
} from '@/types/template'

interface ApiAuthor { id: string; display_name: string }
interface ApiTemplateBase {
  id: string
  template_id: string
  name: string
  category_code: TemplateMarketItem['categoryCode']
  description?: string | null
  tags?: string[]
  file_ext: string
  file_size: number
  version_no: number
  author: ApiAuthor
  save_count: number
  published_at?: string | null
  updated_at: string
  preview_url: string
  cover: {
    status: TemplateMarketItem['cover']['status']
    kind?: TemplateMarketItem['cover']['kind']
    url?: string | null
    spreadsheet?: {
      sheet_name: string
      columns: string[]
      rows: string[][]
      truncated: boolean
    } | null
  }
}
interface ApiMarketTemplate extends ApiTemplateBase { is_saved: boolean }
interface ApiUserTemplate extends ApiTemplateBase {
  visibility: UserTemplateItem['visibility']
  status: string
  source_type: UserTemplateItem['sourceType']
  last_used_at?: string | null
  download_url: string
}
interface ApiPage<T> { items: T[]; total: number; page: number; page_size: number; has_more: boolean }

function mapBase(item: ApiTemplateBase) {
  return {
    id: item.id,
    templateId: item.template_id,
    name: item.name,
    categoryCode: item.category_code,
    description: item.description ?? '',
    tags: item.tags ?? [],
    fileExt: item.file_ext,
    fileSize: item.file_size,
    versionNo: item.version_no,
    author: { id: item.author.id, displayName: item.author.display_name },
    saveCount: item.save_count,
    publishedAt: item.published_at,
    updatedAt: item.updated_at,
    previewUrl: item.preview_url,
    cover: {
      status: item.cover.status,
      kind: item.cover.kind,
      url: item.cover.url,
      spreadsheet: item.cover.spreadsheet ? {
        sheetName: item.cover.spreadsheet.sheet_name,
        columns: item.cover.spreadsheet.columns,
        rows: item.cover.spreadsheet.rows,
        truncated: item.cover.spreadsheet.truncated
      } : null
    }
  }
}

function mapPage<TApi, T>(page: ApiPage<TApi>, mapItem: (item: TApi) => T): TemplatePage<T> {
  return { items: page.items.map(mapItem), total: page.total, page: page.page, pageSize: page.page_size, hasMore: page.has_more }
}

function query(params: Record<string, string | number | undefined>) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== '') search.set(key, String(value)) })
  return search.toString()
}

export async function fetchMarketTemplates(params: { q?: string; category?: string; sort?: string; page?: number; pageSize?: number } = {}) {
  const qs = query({ q: params.q?.trim(), category: params.category ?? 'all', sort: params.sort ?? 'newest', page: params.page ?? 1, page_size: params.pageSize ?? 20 })
  const page = await apiGet<ApiPage<ApiMarketTemplate>>(`/api/templates/market?${qs}`)
  return mapPage(page, (item): TemplateMarketItem => ({ ...mapBase(item), isSaved: item.is_saved }))
}

export async function fetchMyTemplates(params: { q?: string; category?: string; sourceType?: string; page?: number; pageSize?: number } = {}) {
  const qs = query({ q: params.q?.trim(), category: params.category ?? 'all', source_type: params.sourceType ?? 'all', page: params.page ?? 1, page_size: params.pageSize ?? 20 })
  const page = await apiGet<ApiPage<ApiUserTemplate>>(`/api/templates/mine?${qs}`)
  return mapPage(page, (item): UserTemplateItem => ({ ...mapBase(item), visibility: item.visibility, status: item.status, sourceType: item.source_type, lastUsedAt: item.last_used_at, downloadUrl: item.download_url }))
}

export async function uploadTemplate(payload: TemplateUploadPayload): Promise<UserTemplateItem> {
  const form = new FormData()
  form.append('file', payload.file, payload.file.name)
  form.append('name', payload.name.trim())
  form.append('category_code', payload.categoryCode)
  form.append('description', payload.description?.trim() ?? '')
  form.append('tags_json', JSON.stringify(payload.tags ?? []))
  form.append('publish', String(payload.publish ?? false))
  const item = await apiUpload<ApiUserTemplate>('/api/templates/mine', form)
  return { ...mapBase(item), visibility: item.visibility, status: item.status, sourceType: item.source_type, lastUsedAt: item.last_used_at, downloadUrl: item.download_url }
}

export async function saveMarketTemplate(templateId: string) {
  return apiPost<{ success: boolean; already_saved: boolean }>(`/api/templates/market/${encodeURIComponent(templateId)}/save`, {})
}
export async function publishTemplate(userTemplateId: string) { return apiPost(`/api/templates/mine/${encodeURIComponent(userTemplateId)}/publish`, {}) }
export async function unpublishTemplate(userTemplateId: string) { return apiPost(`/api/templates/mine/${encodeURIComponent(userTemplateId)}/unpublish`, {}) }
export async function removeTemplate(userTemplateId: string) { return apiDelete(`/api/templates/mine/${encodeURIComponent(userTemplateId)}`) }
export async function fetchTemplatePreview(templateId: string): Promise<DocumentPreview> { return fetchDocumentPreviewFromUrl(`/api/templates/market/${encodeURIComponent(templateId)}/preview`) }
export async function fetchMyTemplatePreview(userTemplateId: string): Promise<DocumentPreview> { return fetchDocumentPreviewFromUrl(`/api/templates/mine/${encodeURIComponent(userTemplateId)}/preview`) }

export async function downloadTemplate(item: UserTemplateItem) {
  const { blob, filename } = await apiDownloadBlob(item.downloadUrl)
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename || item.name
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally { window.setTimeout(() => URL.revokeObjectURL(url), 1000) }
}

export async function useTemplate(userTemplateId: string, conversationId?: string): Promise<TemplateUseResult> {
  const result = await apiPost<{ conversation: Parameters<typeof mapConversation>[0]; uploaded_file: ApiFile }>(
    `/api/templates/mine/${encodeURIComponent(userTemplateId)}/use`,
    { conversation_id: conversationId ?? null }
  )
  return { conversation: mapConversation(result.conversation), uploadedFile: mapFile(result.uploaded_file) }
}
