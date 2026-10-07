import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import * as projectApi from '@/api/projectApi'
import { useProjectStore } from './projectStore'

vi.mock('@/api/projectApi', () => ({ listProjects: vi.fn(), getProject: vi.fn(), createProject: vi.fn(), updateProject: vi.fn(), deleteProject: vi.fn(), pinProject: vi.fn(), unpinProject: vi.fn(), listProjectSources: vi.fn(), attachProjectSource: vi.fn(), uploadProjectSource: vi.fn(), removeProjectSource: vi.fn(), listProjectArtifacts: vi.fn(), createProjectConversation: vi.fn(), removeConversationFromProject: vi.fn(), moveConversationToProject: vi.fn() }))
vi.mock('@/api/libraryApi', () => ({ renameLibraryItem: vi.fn(), deleteLibraryItem: vi.fn() }))
const detail = { id: 'prj_store', name: 'Store Project', description: '', memoryMode: 'project_memory' as const, pinned: false, updatedAt: 'today', createdAt: '2026-09-09T00:00:00Z', createdByCurrentUser: true, instructions: '', conversations: [], sources: [], artifacts: [] }

describe('Project store', () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks() })
  it('keeps project document sources and conversations synchronized without external knowledge bindings', async () => {
    const store = useProjectStore()
    vi.mocked(projectApi.getProject).mockResolvedValue({ ...detail })
    vi.mocked(projectApi.attachProjectSource).mockResolvedValue({ id: 'src_1', fileId: 'file_1', name: 'requirement.md', description: 'requirement', updatedAt: 'today' })
    vi.mocked(projectApi.createProjectConversation).mockResolvedValue({ id: 'conv_1', title: '任务', summary: '', updatedAt: 'today' })
    vi.mocked(projectApi.pinProject).mockResolvedValue({ ...detail, pinned: true })
    await store.fetchProject(detail.id)
    await store.addLibrarySource({ fileId: 'file_1' })
    await store.createProjectConversation('任务')
    await store.togglePin(detail.id, false)
    expect(store.activeProject).toEqual(expect.objectContaining({ pinned: true }))
    expect(store.projectSources.map((item) => item.id)).toEqual(['src_1'])
    expect(store.projectConversations.map((item) => item.id)).toEqual(['conv_1'])
  })
})
