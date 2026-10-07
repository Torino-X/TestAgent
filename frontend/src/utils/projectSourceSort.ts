import type { ProjectSourcePreview } from '@/types/project'

export type ProjectSourceSort = 'newest' | 'oldest' | 'alphabetical'

function sourceTimestamp(source: ProjectSourcePreview): number {
  const value = Date.parse(source.sortUpdatedAt ?? source.updatedAt)
  return Number.isFinite(value) ? value : 0
}

export function sortProjectSources(
  sources: ProjectSourcePreview[],
  sort: ProjectSourceSort
): ProjectSourcePreview[] {
  return sources
    .map((source, index) => ({ source, index }))
    .sort((left, right) => {
      let order = 0
      if (sort === 'alphabetical') {
        order = left.source.name.localeCompare(right.source.name, 'zh-CN', {
          numeric: true,
          sensitivity: 'base'
        })
      } else {
        order = sourceTimestamp(left.source) - sourceTimestamp(right.source)
        if (sort === 'newest') order *= -1
      }
      return order || left.index - right.index
    })
    .map(({ source }) => source)
}
