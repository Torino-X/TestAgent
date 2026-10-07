import type { LibraryItem, LibraryItemList } from '@/types/library'

const now = new Date()
const daysAgo = (days: number) => new Date(now.getTime() - days * 24 * 60 * 60 * 1000).toISOString()

export const libraryMockItems: LibraryItem[] = [
  {
    id: 'mock-generated-test-plan',
    name: 'TestAgent_测试方案_V1.docx',
    kind: 'file',
    source: 'generated',
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    extension: '.docx',
    sizeBytes: 28672,
    modifiedAt: daysAgo(1),
    artifactType: 'test_plan_word'
  },
  {
    id: 'mock-brief',
    name: '项目需求说明.md',
    kind: 'file',
    source: 'upload',
    mimeType: 'text/markdown',
    extension: '.md',
    sizeBytes: 12083,
    modifiedAt: daysAgo(2)
  },
  {
    id: 'mock-note',
    name: '粘贴的文本 (1).txt',
    kind: 'file',
    source: 'upload',
    mimeType: 'text/plain',
    extension: '.txt',
    sizeBytes: 22835,
    modifiedAt: daysAgo(3)
  },
  {
    id: 'mock-image',
    name: '测试流程草图.png',
    kind: 'image',
    source: 'upload',
    mimeType: 'image/png',
    extension: '.png',
    sizeBytes: 157696,
    modifiedAt: daysAgo(3)
  }
]

export function getMockLibraryItems(category: 'all' | 'image' | 'file', query = ''): LibraryItemList {
  const keyword = query.trim().toLocaleLowerCase()
  const items = libraryMockItems.filter((item) =>
    (category === 'all' || item.kind === category) &&
    (!keyword || item.name.toLocaleLowerCase().includes(keyword))
  )
  return { items, total: items.length, page: 1, pageSize: items.length || 50, hasMore: false }
}
