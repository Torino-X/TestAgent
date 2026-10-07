import { afterEach, describe, expect, it, vi } from 'vitest'
import { attachProjectSource, createProject, deleteProject, getProject, listProjects, moveConversationToProject, pinProject, removeConversationFromProject, removeProjectSource, updateProject, uploadProjectSource } from './projectApi'

function envelope(data: unknown) { return new Response(JSON.stringify({ code: 0, message: 'success', data }), { status: 200, headers: { 'Content-Type': 'application/json' } }) }
const summary = { id: 'prj_real_1', name: '项目', description: '', memoryMode: 'project_memory' as const, pinned: false, updatedAt: '2026-09-08T09:00:00Z', createdAt: '2026-09-08T09:00:00Z', createdByCurrentUser: true }
const detail = { ...summary, instructions: '', conversations: [], sources: [], artifacts: [] }

describe('Project API contract', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('uses project source endpoints without external knowledge bindings', async () => {
    const source = { id: 'src_1', fileId: 'file_1', name: 'requirement.md', description: 'requirement', updatedAt: '2026-09-08T09:00:00Z' }
    const moved = { id: 'conv_1', title: '会话', summary: '', updatedAt: '2026-09-08T09:00:00Z' }
    const fetchMock = vi.fn().mockResolvedValueOnce(envelope({ items: [summary], total: 1 })).mockResolvedValueOnce(envelope(detail)).mockResolvedValueOnce(envelope(detail)).mockResolvedValueOnce(envelope({ ...summary, pinned: true })).mockResolvedValueOnce(envelope(moved)).mockResolvedValueOnce(envelope(null)).mockResolvedValueOnce(envelope(source)).mockResolvedValueOnce(envelope(source)).mockResolvedValueOnce(envelope(null)).mockResolvedValueOnce(envelope(detail)).mockResolvedValueOnce(envelope(null))
    vi.stubGlobal('fetch', fetchMock)
    await listProjects({ q: 'project', scope: 'created', page: 2, pageSize: 10 })
    await createProject({ name: '项目', memoryMode: 'project_memory' })
    await updateProject(summary.id, { description: 'updated' })
    await pinProject(summary.id)
    await moveConversationToProject(summary.id, 'conv_1')
    await removeConversationFromProject('conv_1')
    await attachProjectSource(summary.id, { fileId: 'file_1', sourceRole: 'requirement' })
    const file = Object.assign(new Blob(['prd'], { type: 'text/markdown' }), { name: 'requirement.md', lastModified: 0 }) as File
    await uploadProjectSource(summary.id, file)
    await removeProjectSource(summary.id, 'src_1')
    await getProject(summary.id)
    await deleteProject(summary.id)
    expect(fetchMock.mock.calls.map((call) => String(call[0])).some((url) => url.includes('knowledge-connections'))).toBe(false)
  })
})
