export type ProjectMemoryMode = 'project_memory' | 'none'
export type ProjectListScope = 'all' | 'created' | 'shared'

export interface ProjectConversationPreview {
  id: string
  title: string
  summary: string
  updatedAt: string
}

export interface ProjectSourcePreview {
  id: string
  fileId?: string
  name: string
  description: string
  sourceRole?: string
  isCurrent?: boolean
  addedFrom?: string
  updatedAt: string
  sortUpdatedAt?: string
}

export interface ProjectArtifactPreview {
  id: string
  name: string
  type: '测试方案' | '测试用例' | '审查报告' | '其他资产'
  updatedAt: string
}

export interface ProjectPreview {
  id: string
  name: string
  description?: string
  updatedAt: string
  createdByCurrentUser: boolean
  pinned?: boolean
  memoryMode: ProjectMemoryMode
  conversations: ProjectConversationPreview[]
  sources: ProjectSourcePreview[]
  artifacts: ProjectArtifactPreview[]
}

export interface ProjectSummary {
  id: string
  name: string
  description: string
  memoryMode: ProjectMemoryMode
  pinned: boolean
  updatedAt: string
  createdAt: string
  createdByCurrentUser: boolean
}

export interface ProjectDetail extends ProjectSummary {
  instructions: string
  conversations: ProjectConversationPreview[]
  sources: ProjectSourcePreview[]
  artifacts: ProjectArtifactPreview[]
}

export interface CreateProjectPayload {
  name: string
  description?: string
  memoryMode?: ProjectMemoryMode
}

export interface UpdateProjectPayload {
  name?: string
  description?: string
  memoryMode?: ProjectMemoryMode
  instructions?: string
}

export interface AttachProjectSourcePayload {
  fileId: string
  sourceRole?: 'requirement' | 'api' | 'design' | 'history' | 'other'
}
