import { apiDelete, apiGet, apiPatch, apiPost, apiPut, apiUpload } from './request'
import type {
  AttachProjectSourcePayload,
  CreateProjectPayload,
  ProjectArtifactPreview,
  ProjectConversationPreview,
  ProjectDetail,
  ProjectListScope,
  ProjectSourcePreview,
  ProjectSummary,
  UpdateProjectPayload
} from '@/types/project'

export interface ListProjectsParams {
  q?: string
  scope?: ProjectListScope
  page?: number
  pageSize?: number
}

export interface ProjectListResponse {
  items: ProjectSummary[]
  total: number
}

interface ApiPage<T> {
  items: T[]
  total: number
}

interface InstructionsResponse {
  instructions: string
}

function projectPath(projectId: string, suffix = ''): string {
  return `/api/projects/${encodeURIComponent(projectId)}${suffix}`
}

function displayDate(value: string): string {
  const parsed = new Date(value)
  if (!value || Number.isNaN(parsed.getTime())) return value || '刚刚'
  const now = new Date()
  if (parsed.getFullYear() === now.getFullYear() && parsed.getMonth() === now.getMonth() && parsed.getDate() === now.getDate()) {
    return '今天'
  }
  return parsed.toLocaleDateString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    ...(parsed.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' })
  })
}

function mapConversation(item: ProjectConversationPreview): ProjectConversationPreview {
  return { ...item, summary: item.summary ?? '', updatedAt: displayDate(item.updatedAt) }
}

function mapSource(item: ProjectSourcePreview): ProjectSourcePreview {
  const sortUpdatedAt = item.sortUpdatedAt ?? item.updatedAt
  return { ...item, description: item.description ?? '', sortUpdatedAt, updatedAt: displayDate(item.updatedAt) }
}

function mapArtifact(item: ProjectArtifactPreview): ProjectArtifactPreview {
  return { ...item, updatedAt: displayDate(item.updatedAt) }
}

function mapSummary(item: ProjectSummary): ProjectSummary {
  return {
    ...item,
    description: item.description ?? '',
    pinned: Boolean(item.pinned),
    createdByCurrentUser: item.createdByCurrentUser !== false,
    updatedAt: displayDate(item.updatedAt)
  }
}

function mapDetail(item: ProjectDetail): ProjectDetail {
  return {
    ...mapSummary(item),
    instructions: item.instructions ?? '',
    conversations: (item.conversations ?? []).map(mapConversation),
    sources: (item.sources ?? []).map(mapSource),
    artifacts: (item.artifacts ?? []).map(mapArtifact)
  }
}

export async function listProjects(params: ListProjectsParams = {}): Promise<ProjectListResponse> {
  const query = new URLSearchParams({
    scope: params.scope ?? 'all',
    page: String(params.page ?? 1),
    page_size: String(params.pageSize ?? 20)
  })
  if (params.q?.trim()) query.set('q', params.q.trim())
  const response = await apiGet<ApiPage<ProjectSummary>>(`/api/projects?${query.toString()}`)
  return { items: response.items.map(mapSummary), total: response.total }
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  return mapDetail(await apiGet<ProjectDetail>(projectPath(projectId)))
}

export async function createProject(payload: CreateProjectPayload): Promise<ProjectDetail> {
  return mapDetail(await apiPost<ProjectDetail>('/api/projects', payload))
}

export async function updateProject(projectId: string, payload: UpdateProjectPayload): Promise<ProjectDetail> {
  return mapDetail(await apiPatch<ProjectDetail>(projectPath(projectId), payload))
}

export async function deleteProject(projectId: string): Promise<void> {
  await apiDelete(projectPath(projectId))
}

export async function pinProject(projectId: string): Promise<ProjectSummary> {
  return mapSummary(await apiPost<ProjectSummary>(projectPath(projectId, '/pin'), {}))
}

export async function unpinProject(projectId: string): Promise<ProjectSummary> {
  return mapSummary(await apiDelete<ProjectSummary>(projectPath(projectId, '/pin')))
}

export async function listProjectConversations(projectId: string): Promise<ProjectConversationPreview[]> {
  const response = await apiGet<ApiPage<ProjectConversationPreview>>(projectPath(projectId, '/conversations'))
  return response.items.map(mapConversation)
}

export async function createProjectConversation(projectId: string, title: string): Promise<ProjectConversationPreview> {
  return mapConversation(await apiPost<ProjectConversationPreview>(projectPath(projectId, '/conversations'), { title }))
}

export async function moveConversationToProject(projectId: string, conversationId: string): Promise<ProjectConversationPreview> {
  return mapConversation(await apiPost<ProjectConversationPreview>(`/api/conversations/${encodeURIComponent(conversationId)}/project`, { projectId }))
}

export async function removeConversationFromProject(conversationId: string): Promise<void> {
  await apiDelete(`/api/conversations/${encodeURIComponent(conversationId)}/project`)
}

export async function listProjectSources(projectId: string): Promise<ProjectSourcePreview[]> {
  const response = await apiGet<ApiPage<ProjectSourcePreview>>(projectPath(projectId, '/sources'))
  return response.items.map(mapSource)
}

export async function attachProjectSource(projectId: string, payload: AttachProjectSourcePayload): Promise<ProjectSourcePreview> {
  return mapSource(await apiPost<ProjectSourcePreview>(projectPath(projectId, '/sources/attach'), payload))
}

export async function uploadProjectSource(projectId: string, file: File): Promise<ProjectSourcePreview> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('sourceRole', 'other')
  return mapSource(await apiUpload<ProjectSourcePreview>(projectPath(projectId, '/sources/upload'), formData))
}

export async function removeProjectSource(projectId: string, sourceId: string): Promise<void> {
  await apiDelete(projectPath(projectId, `/sources/${encodeURIComponent(sourceId)}`))
}

export async function listProjectArtifacts(projectId: string): Promise<ProjectArtifactPreview[]> {
  const response = await apiGet<ApiPage<ProjectArtifactPreview>>(projectPath(projectId, '/artifacts'))
  return response.items.map(mapArtifact)
}

export async function getProjectInstructions(projectId: string): Promise<string> {
  return (await apiGet<InstructionsResponse>(projectPath(projectId, '/instructions'))).instructions
}

export async function updateProjectInstructions(projectId: string, instructions: string): Promise<string> {
  return (await apiPut<InstructionsResponse>(projectPath(projectId, '/instructions'), { instructions })).instructions
}
