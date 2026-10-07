import type { Conversation, FileAttachment } from '@/types'
import type { DocumentPreview, SpreadsheetPreview } from '@/types/library'

export type TemplateCategory = 'all' | 'test_plan' | 'test_case' | 'defect_analysis_report' | 'other'
export type TemplateSourceType = 'all' | 'owner' | 'market_saved'
export type TemplateSort = 'newest' | 'saves'

export interface TemplateAuthor {
  id: string
  displayName: string
}

export interface TemplateCover {
  status: 'missing' | 'queued' | 'processing' | 'ready' | 'failed'
  kind?: 'pdf' | 'spreadsheet' | null
  url?: string | null
  spreadsheet?: SpreadsheetPreview | null
}

export interface TemplateMarketItem {
  id: string
  templateId: string
  name: string
  categoryCode: Exclude<TemplateCategory, 'all'>
  description: string
  tags: string[]
  fileExt: string
  fileSize: number
  versionNo: number
  author: TemplateAuthor
  saveCount: number
  isSaved: boolean
  publishedAt?: string | null
  updatedAt: string
  previewUrl: string
  cover: TemplateCover
}

export interface UserTemplateItem extends Omit<TemplateMarketItem, 'isSaved'> {
  visibility: 'private' | 'public'
  status: string
  sourceType: Exclude<TemplateSourceType, 'all'>
  lastUsedAt?: string | null
  downloadUrl: string
}

export interface TemplatePage<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
  hasMore: boolean
}

export interface TemplateUploadPayload {
  file: File
  name: string
  categoryCode: Exclude<TemplateCategory, 'all'>
  description?: string
  tags?: string[]
  publish?: boolean
}

export interface TemplateUseResult {
  conversation: Conversation
  uploadedFile: FileAttachment
}

export type TemplatePreview = DocumentPreview

export const TEMPLATE_CATEGORIES: Array<{ value: TemplateCategory; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'test_plan', label: '测试方案' },
  { value: 'test_case', label: '测试用例' },
  { value: 'defect_analysis_report', label: '缺陷分析报告' },
  { value: 'other', label: '其他' }
]

export function templateCategoryLabel(category: string): string {
  return TEMPLATE_CATEGORIES.find((item) => item.value === category)?.label ?? '其他'
}
