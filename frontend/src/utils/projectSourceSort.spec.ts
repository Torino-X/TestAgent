import { describe, expect, it } from 'vitest'
import { sortProjectSources, type ProjectSourceSort } from './projectSourceSort'
import type { ProjectSourcePreview } from '@/types/project'

const sources: ProjectSourcePreview[] = [
  { id: 'src_b', name: 'Beta', description: '', updatedAt: '9月8日', sortUpdatedAt: '2026-09-08T09:00:00Z' },
  { id: 'src_c', name: 'Charlie', description: '', updatedAt: '今天', sortUpdatedAt: '2026-09-09T09:00:00Z' },
  { id: 'src_a', name: 'Alpha', description: '', updatedAt: '9月7日', sortUpdatedAt: '2026-09-07T09:00:00Z' }
]

describe('Project source sorting', () => {
  it.each<[ProjectSourceSort, string[]]>([
    ['newest', ['src_c', 'src_b', 'src_a']],
    ['oldest', ['src_a', 'src_b', 'src_c']],
    ['alphabetical', ['src_a', 'src_b', 'src_c']]
  ])('sorts sources by %s without mutating store order', (sort, ids) => {
    const original = sources.map((source) => source.id)

    expect(sortProjectSources(sources, sort).map((source) => source.id)).toEqual(ids)
    expect(sources.map((source) => source.id)).toEqual(original)
  })
})
