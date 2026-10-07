import { ApiRequestError, apiDownloadBlob, apiGet } from './request'
import type { DocumentPreview, LibraryItem, SpreadsheetPreview } from '@/types/library'

interface ApiPreviewItem {
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

interface ApiPreviewResponse {
  item: ApiPreviewItem
  viewer: DocumentPreview['viewer']
  text_content?: string | null
  spreadsheet?: { sheet_name: string; columns: string[]; rows: string[][]; truncated: boolean } | null
  pdf_url?: string | null
  preview_status?: DocumentPreview['previewStatus']
  preview_error?: string | null
}

function mapItem(item: ApiPreviewItem): LibraryItem {
  return {
    id: item.id, name: item.name, kind: item.kind, source: item.source,
    mimeType: item.mime_type, extension: item.extension ?? '', sizeBytes: item.size_bytes,
    modifiedAt: item.modified_at, conversationId: item.conversation_id,
    artifactType: item.artifact_type, thumbnailUrl: item.thumbnail_url,
    downloadUrl: item.download_url, deletedAt: item.deleted_at
  }
}

function mapSpreadsheet(value: ApiPreviewResponse['spreadsheet']): SpreadsheetPreview | null {
  if (!value) return null
  return { sheetName: value.sheet_name, columns: value.columns, rows: value.rows, truncated: value.truncated }
}

export async function fetchDocumentPreview(itemId: string): Promise<DocumentPreview> {
  return fetchDocumentPreviewFromUrl(`/api/library/items/${encodeURIComponent(itemId)}/preview`)
}

export async function fetchDocumentPreviewFromUrl(url: string): Promise<DocumentPreview> {
  let payload: ApiPreviewResponse
  try {
    payload = await apiGet<ApiPreviewResponse>(url)
  } catch (error) {
    // A response interrupted by a failed preview background task is surfaced
    // by fetch as a transport TypeError. It is not evidence that the backend
    // process is stopped, so keep the recovery guidance preview-specific.
    if (error instanceof ApiRequestError && error.status === 0) {
      throw new ApiRequestError('文档预览请求被中断，请稍后重试。', error.code, error.status, error.requestId)
    }
    throw error
  }
  return {
    item: mapItem(payload.item), viewer: payload.viewer, textContent: payload.text_content,
    spreadsheet: mapSpreadsheet(payload.spreadsheet), pdfUrl: payload.pdf_url,
    previewStatus: payload.preview_status, previewError: payload.preview_error
  }
}

export async function fetchPreviewPdf(url: string): Promise<Blob> {
  const { blob } = await apiDownloadBlob(url)
  return blob
}
