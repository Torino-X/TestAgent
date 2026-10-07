export type LibraryCategory = 'all' | 'image' | 'file'
export type LibraryScope = 'active' | 'deleted'
export type LibrarySourceFilter = 'all' | 'upload' | 'generated'
export type LibraryFileType = 'all' | 'image' | 'document' | 'spreadsheet' | 'presentation' | 'pdf'
export type LibraryItemKind = Exclude<LibraryCategory, 'all'>
export type LibraryItemSource = 'upload' | 'generated' | 'template'

export interface LibraryItem {
  id: string
  name: string
  kind: LibraryItemKind
  source: LibraryItemSource
  mimeType?: string | null
  extension: string
  sizeBytes?: number | null
  modifiedAt: string
  conversationId?: string | null
  artifactType?: string | null
  thumbnailUrl?: string | null
  downloadUrl?: string | null
  deletedAt?: string | null
}

export interface LibraryItemList {
  items: LibraryItem[]
  total: number
  page: number
  pageSize: number
  hasMore: boolean
}

export type DocumentPreviewKind = 'word' | 'spreadsheet' | 'pdf' | 'markdown' | 'text' | 'unsupported'
export type DocumentPreviewStatus = 'queued' | 'processing' | 'ready' | 'failed'

export interface SpreadsheetPreview {
  sheetName: string
  columns: string[]
  rows: string[][]
  truncated: boolean
}

export interface DocumentPreview {
  item: LibraryItem
  viewer: DocumentPreviewKind
  textContent?: string | null
  spreadsheet?: SpreadsheetPreview | null
  pdfUrl?: string | null
  previewStatus?: DocumentPreviewStatus | null
  previewError?: string | null
}
