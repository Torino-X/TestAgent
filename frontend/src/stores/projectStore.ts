import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import * as projectApi from '@/api/projectApi'
import { deleteLibraryItem, renameLibraryItem } from '@/api/libraryApi'
import type {
  AttachProjectSourcePayload,
  CreateProjectPayload,
  ProjectConversationPreview,
  ProjectDetail,
  ProjectListScope,
  ProjectSummary,
  UpdateProjectPayload
} from '@/types/project'

type ProjectErrorKey = 'list' | 'detail' | 'source' | 'artifact' | 'mutation'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '项目操作失败，请稍后重试。'
}

function toSummary(project: ProjectSummary): ProjectSummary {
  return {
    id: project.id,
    name: project.name,
    description: project.description,
    memoryMode: project.memoryMode,
    pinned: project.pinned,
    updatedAt: project.updatedAt,
    createdAt: project.createdAt,
    createdByCurrentUser: project.createdByCurrentUser
  }
}

export const useProjectStore = defineStore('projects', () => {
  const projects = ref<ProjectSummary[]>([])
  const activeProject = ref<ProjectDetail | null>(null)
  const projectConversations = computed(() => activeProject.value?.conversations ?? [])
  const projectSources = computed(() => activeProject.value?.sources ?? [])
  const projectArtifacts = computed(() => activeProject.value?.artifacts ?? [])
  const projectKnowledgeConnections = computed<string[]>(() => [])
  const loading = reactive({ list: false, detail: false, source: false, artifact: false, mutating: false })
  const errors = reactive<Record<ProjectErrorKey, string>>({ list: '', detail: '', source: '', artifact: '', mutation: '' })

  function clearError(key: ProjectErrorKey) { errors[key] = '' }
  function setError(key: ProjectErrorKey, error: unknown) { errors[key] = errorMessage(error) }

  function syncSummary(project: ProjectSummary) {
    const summary = toSummary(project)
    const index = projects.value.findIndex((item) => item.id === summary.id)
    projects.value = index < 0
      ? [summary, ...projects.value]
      : projects.value.map((item) => item.id === summary.id ? summary : item)
  }

  async function fetchProjects(params: { q?: string; scope?: ProjectListScope } = {}) {
    loading.list = true
    clearError('list')
    try {
      const response = await projectApi.listProjects(params)
      projects.value = response.items
      return response
    } catch (error) {
      setError('list', error)
      throw error
    } finally {
      loading.list = false
    }
  }

  async function fetchProject(projectId: string) {
    loading.detail = true
    clearError('detail')
    if (activeProject.value?.id !== projectId) activeProject.value = null
    try {
      const project = await projectApi.getProject(projectId)
      activeProject.value = project
      syncSummary(project)
      return project
    } catch (error) {
      activeProject.value = null
      setError('detail', error)
      throw error
    } finally {
      loading.detail = false
    }
  }

  async function createProject(payload: CreateProjectPayload) {
    loading.mutating = true
    clearError('mutation')
    try {
      const project = await projectApi.createProject(payload)
      activeProject.value = project
      syncSummary(project)
      return project
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  async function updateActiveProject(payload: UpdateProjectPayload) {
    if (!activeProject.value) throw new Error('请先选择项目。')
    loading.mutating = true
    clearError('mutation')
    try {
      const project = await projectApi.updateProject(activeProject.value.id, payload)
      activeProject.value = project
      syncSummary(project)
      return project
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  async function deleteProject(projectId: string) {
    loading.mutating = true
    clearError('mutation')
    try {
      await projectApi.deleteProject(projectId)
      projects.value = projects.value.filter((item) => item.id !== projectId)
      if (activeProject.value?.id === projectId) activeProject.value = null
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  async function togglePin(projectId: string, pinned: boolean) {
    loading.mutating = true
    clearError('mutation')
    try {
      const summary = pinned ? await projectApi.unpinProject(projectId) : await projectApi.pinProject(projectId)
      if (activeProject.value?.id === summary.id) activeProject.value = { ...activeProject.value, ...summary }
      syncSummary(summary)
      return summary
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  async function fetchProjectSources() {
    if (!activeProject.value) return []
    loading.source = true
    clearError('source')
    try {
      const sources = await projectApi.listProjectSources(activeProject.value.id)
      activeProject.value.sources = sources
      return sources
    } catch (error) {
      setError('source', error)
      throw error
    } finally {
      loading.source = false
    }
  }

  async function addLibrarySource(payload: AttachProjectSourcePayload) {
    if (!activeProject.value) throw new Error('请先选择项目。')
    loading.source = true
    clearError('source')
    try {
      const source = await projectApi.attachProjectSource(activeProject.value.id, payload)
      activeProject.value.sources = [source, ...activeProject.value.sources.filter((item) => item.id !== source.id)]
      return source
    } catch (error) {
      setError('source', error)
      throw error
    } finally {
      loading.source = false
    }
  }

  async function uploadSource(file: File) {
    if (!activeProject.value) throw new Error('请先选择项目。')
    loading.source = true
    clearError('source')
    try {
      const source = await projectApi.uploadProjectSource(activeProject.value.id, file)
      activeProject.value.sources = [source, ...activeProject.value.sources.filter((item) => item.id !== source.id)]
      return source
    } catch (error) {
      setError('source', error)
      throw error
    } finally {
      loading.source = false
    }
  }

  async function removeSource(sourceId: string) {
    if (!activeProject.value) return
    loading.source = true
    clearError('source')
    try {
      await projectApi.removeProjectSource(activeProject.value.id, sourceId)
      activeProject.value.sources = activeProject.value.sources.filter((item) => item.id !== sourceId)
    } catch (error) {
      setError('source', error)
      throw error
    } finally {
      loading.source = false
    }
  }

  async function fetchProjectArtifacts() {
    if (!activeProject.value) return []
    loading.artifact = true
    clearError('artifact')
    try {
      const artifacts = await projectApi.listProjectArtifacts(activeProject.value.id)
      activeProject.value.artifacts = artifacts
      return artifacts
    } catch (error) {
      setError('artifact', error)
      throw error
    } finally {
      loading.artifact = false
    }
  }

  async function renameProjectArtifact(artifactId: string, name: string) {
    if (!activeProject.value) throw new Error('请先选择项目。')
    loading.artifact = true
    clearError('artifact')
    try {
      const renamed = await renameLibraryItem(artifactId, name)
      activeProject.value.artifacts = activeProject.value.artifacts.map((artifact) => artifact.id === artifactId
        ? { ...artifact, name: renamed.name, updatedAt: '今天' }
        : artifact)
      return renamed
    } catch (error) {
      setError('artifact', error)
      throw error
    } finally {
      loading.artifact = false
    }
  }

  async function deleteProjectArtifact(artifactId: string) {
    if (!activeProject.value) throw new Error('请先选择项目。')
    loading.artifact = true
    clearError('artifact')
    try {
      await deleteLibraryItem(artifactId)
      activeProject.value.artifacts = activeProject.value.artifacts.filter((artifact) => artifact.id !== artifactId)
    } catch (error) {
      setError('artifact', error)
      throw error
    } finally {
      loading.artifact = false
    }
  }

  async function createProjectConversation(title: string) {
    if (!activeProject.value) throw new Error('请先选择项目。')
    loading.mutating = true
    clearError('mutation')
    try {
      const conversation = await projectApi.createProjectConversation(activeProject.value.id, title)
      activeProject.value.conversations = [conversation, ...activeProject.value.conversations]
      return conversation
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  async function removeProjectConversation(conversationId: string) {
    if (!activeProject.value) return
    loading.mutating = true
    clearError('mutation')
    try {
      await projectApi.removeConversationFromProject(conversationId)
      activeProject.value.conversations = activeProject.value.conversations.filter((item) => item.id !== conversationId)
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  async function moveConversationToProject(projectId: string, conversation: Pick<ProjectConversationPreview, 'id'>) {
    loading.mutating = true
    clearError('mutation')
    try {
      const moved = await projectApi.moveConversationToProject(projectId, conversation.id)
      if (activeProject.value?.id === projectId && !activeProject.value.conversations.some((item) => item.id === moved.id)) {
        activeProject.value.conversations = [moved, ...activeProject.value.conversations]
      }
      return moved
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  async function saveKnowledgeConnections(knowledgeIds: string[]) {
    if (!activeProject.value) throw new Error('请先选择项目。')
    loading.mutating = true
    clearError('mutation')
    try {
      return knowledgeIds
    } catch (error) {
      setError('mutation', error)
      throw error
    } finally {
      loading.mutating = false
    }
  }

  return {
    projects,
    activeProject,
    projectConversations,
    projectSources,
    projectArtifacts,
    projectKnowledgeConnections,
    loading,
    errors,
    fetchProjects,
    fetchProject,
    createProject,
    updateActiveProject,
    deleteProject,
    togglePin,
    fetchProjectSources,
    addLibrarySource,
    uploadSource,
    removeSource,
    fetchProjectArtifacts,
    renameProjectArtifact,
    deleteProjectArtifact,
    createProjectConversation,
    removeProjectConversation,
    moveConversationToProject,
    saveKnowledgeConnections
  }
})
