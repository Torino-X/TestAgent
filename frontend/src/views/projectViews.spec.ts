import { describe, expect, it } from 'vitest'
import { mockProjects } from '@/mocks/projects'
import { routes } from '@/router/routes'
import projectDetailSource from './ProjectDetailView.vue?raw'
import projectListSource from './ProjectListView.vue?raw'
import appLayoutSource from '@/components/layout/AppLayout.vue?raw'
import sidebarSource from '@/components/layout/Sidebar.vue?raw'
import projectSettingsSource from '@/components/projects/ProjectSettingsDialog.vue?raw'
import createProjectSource from '@/components/projects/CreateProjectDialog.vue?raw'
import projectSelectSource from '@/components/projects/ProjectSelect.vue?raw'
import appSelectSource from '@/components/common/AppSelect.vue?raw'
import conversationListSource from '@/components/layout/ConversationList.vue?raw'
import projectSourcePickerSource from '@/components/projects/ProjectSourcePicker.vue?raw'
import projectApiSource from '@/api/projectApi.ts?raw'
import projectStoreSource from '@/stores/projectStore.ts?raw'
import chatWorkspaceSource from '@/components/chat/ChatWorkspace.vue?raw'

describe('Project frontend skeleton', () => {
  it('exposes the standalone list and workspace routes without changing chat routes', () => {
    expect(routes.find((route) => route.path === '/projects')?.name).toBe('projects')
    expect(routes.find((route) => route.path === '/projects/:projectId')?.name).toBe('projects-detail')
    expect(routes.find((route) => route.path === '/chat/:conversationId')?.name).toBe('chat-detail')
  })

  it('provides mock workspace data for the list, chat, source, and artifact views', () => {
    expect(mockProjects.length).toBeGreaterThan(0)
    expect(mockProjects[0]).toEqual(expect.objectContaining({
      id: expect.stringMatching(/^prj_/),
      name: expect.any(String),
      conversations: expect.any(Array),
      sources: expect.any(Array),
      artifacts: expect.any(Array)
    }))
  })

it('keeps the Project workspace inside the desktop application shell without unsupported composer controls', () => {
  expect(appLayoutSource).toContain('app-shell--project')
  expect(appLayoutSource).toContain('force-visible')
  expect(projectDetailSource).toContain("import AppLayout from '@/components/layout/AppLayout.vue'")
  expect(sidebarSource).toContain('shouldForceVisible')
  expect(sidebarSource).toContain('props.forceVisible')
  expect(projectDetailSource).toContain('min-height: 100vh')
  expect(projectDetailSource).not.toContain('project-composer__priority')
    expect(projectDetailSource).not.toContain('MicOutline')
  expect(projectDetailSource).not.toContain('VolumeHighOutline')
  })

  it('opens the same project settings dialog from the workspace and project-list menu', () => {
    expect(projectListSource).toContain('ProjectSettingsDialog')
    expect(projectListSource).toContain("action === 'settings'")
    expect(projectListSource).toContain('settingsOpen.value = true')
    expect(projectSettingsSource).toContain('库访问权限')
    expect(projectSettingsSource).toContain('删除项目')
  })

  it('uses real APIs for Phase 5 flows and renders recoverable asynchronous states', () => {
    expect(projectApiSource).not.toContain("@/mocks/projects")
    expect(projectApiSource).toContain("from './request'")
    expect(projectListSource).toContain('projectStore.errors.list')
    expect(projectDetailSource).toContain('projectStore.errors.detail')
    expect(projectDetailSource).toContain('conversationStore.queueInitialMessage(conversation.id, text)')
    expect(projectDetailSource).toContain('removeProjectConversation')
    expect(projectSourcePickerSource).toContain("source: 'upload'")
    expect(projectSourcePickerSource).toContain("emit('attach', item.id)")
  })

  it('hands a new Project chat to the normal streaming workspace and renders a send action', () => {
    expect(projectDetailSource).toContain('ArrowUpOutline')
    expect(projectDetailSource).toContain('project-composer__send')
    expect(projectDetailSource).toContain('@click="startProjectChat"')
    expect(projectDetailSource).toContain(':disabled="!newChatText.trim() || startingChat"')
    expect(projectDetailSource).toContain('conversationStore.queueInitialMessage(conversation.id, text)')
    expect(projectDetailSource).toContain('conversationStore.mergeConversationSummary')
    expect(projectDetailSource).toContain('await router.push(`/chat/${encodeURIComponent(conversation.id)}`)')
    expect(projectDetailSource).not.toContain("import { sendMessage } from '@/api/messageApi'")
  })

  it('matches the Phase 7 Project list, settings, source copy, and source sort controls', () => {
    expect(projectListSource).toContain('.project-row:last-child')
    expect(projectSettingsSource).toMatch(/\.project-settings\s*\{[^}]*overflow:\s*visible/)
    expect(projectSettingsSource).not.toMatch(/\.project-settings\s*\{[^}]*overflow-y:\s*auto/)
    expect(projectDetailSource).toContain("{ label: '项目资料', value: 'sources' as const }")
    expect(projectDetailSource).toContain('为TestAgent提供更多背景信息')
    expect(projectDetailSource).toContain(':options="sourceSortOptions"')
    expect(projectDetailSource).toContain('v-for="source in sortedSources"')
    expect(projectDetailSource).toContain('project-source-sort-menu')
    expect(projectDetailSource).not.toContain('<button type="button">全部')
    expect(projectDetailSource).toContain('conversationStore.setConversationProject(conversationId, null)')
  })

  it('matches the Phase 8 Project icons, placeholder, and custom dropdown surfaces', () => {
    expect(conversationListSource).toContain('<n-icon :component="FolderOutline"')
    expect(projectListSource).toContain("import pinIconUrl from '@/assets/icons/pin.svg'")
    expect(projectListSource).toContain("class: 'project-menu-pin-icon'")
    expect(projectListSource).toContain("class: 'project-menu-option__icon'")
    expect(projectListSource).toContain('.project-row-menu .n-dropdown-option-body__prefix { display: none;')
    expect(createProjectSource).toContain('placeholder="请输入项目名称"')
    expect(createProjectSource).toContain('<ProjectSelect')
    expect(projectSettingsSource).toContain('<ProjectSelect')
    expect(createProjectSource).not.toContain('<select')
    expect(projectSettingsSource).not.toContain('<select')
    expect(projectSelectSource).toContain('<AppSelect')
    expect(appSelectSource).toContain('app-select-option__description')
    expect(appSelectSource).toContain('CheckmarkOutline')
    expect(appSelectSource).toContain('app-select-menu__option--selected')
  })

  it('renders functional Project artifact rows with colored icons, centered dates, and file actions', () => {
    expect(projectDetailSource).toContain("import { resolveFileIcon } from '@/utils/fileIcon'")
    expect(projectDetailSource).toContain('project-artifact__file-icon')
    expect(projectDetailSource).toContain('project-artifact__file-icon--word')
    expect(projectDetailSource).toContain('project-artifact__time')
    expect(projectDetailSource).toContain(':options="artifactActionOptions"')
    expect(projectDetailSource).toContain("iconOption('下载', 'download', DownloadOutline)")
    expect(projectDetailSource).toContain("iconOption('重命名', 'rename', CreateOutline)")
    expect(projectDetailSource).toContain("iconOption('删除', 'delete', TrashOutline, true)")
    expect(projectDetailSource).toContain('downloadArtifact(artifact.id, artifact.name)')
    expect(projectDetailSource).toContain('projectStore.renameProjectArtifact')
    expect(projectDetailSource).toContain('projectStore.deleteProjectArtifact')
    expect(projectStoreSource).toContain('renameLibraryItem')
    expect(projectStoreSource).toContain('deleteLibraryItem')
  })

  it('opens a Project artifact in the shared document preview without hijacking its action menu', () => {
    expect(projectDetailSource).toContain('role="button" tabindex="0"')
    expect(projectDetailSource).toContain('@click="openArtifactPreview(artifact)"')
    expect(projectDetailSource).toContain('@keydown.enter.space.prevent="openArtifactPreview(artifact)"')
    expect(projectDetailSource).toContain("name: 'document-preview'")
    expect(projectDetailSource).toContain("from: 'project'")
    expect(projectDetailSource).toContain('@click.stop')
  })

  it('renders Project files as rounded colored-icon rows and opens the underlying Library file', () => {
    expect(projectDetailSource).toContain('class="project-source project-source--interactive"')
    expect(projectDetailSource).toContain('project-source__file-icon')
    expect(projectDetailSource).toContain('function sourceIcon(name: string) { return resolveFileIcon(name) }')
    expect(projectDetailSource).toContain('@click="openSourcePreview(source)"')
    expect(projectDetailSource).toContain('params: { itemId: source.fileId }')
    expect(projectDetailSource).toContain('.project-source:hover, .project-source:focus-within { background: #f5f5f5;')
    expect(projectDetailSource).toContain('border-radius: 11px')
    expect(projectDetailSource).toContain('@click.stop="removeSource(source.id)"')
  })

  it('keeps the add-project-material path available above a non-empty source list', () => {
    expect(projectDetailSource).toContain('class="project-source-add"')
    expect(projectDetailSource).toContain('添加项目资料')
  })

  it('uses fixed icon columns for Project asset actions', () => {
    expect(projectDetailSource).toContain("class: 'project-artifact-action-option__icon'")
    expect(projectDetailSource).toContain('.project-artifact-action-menu .n-dropdown-option-body__prefix { display: none;')
    expect(projectDetailSource).toContain('.project-artifact-action-option__icon { display: grid; flex: 0 0 18px;')
    expect(projectDetailSource).not.toContain("icon: () => h(NIcon")
  })

  it('treats the Project composer plus button as a one-message attachment picker', () => {
    expect(projectDetailSource).toContain('ref="chatFileInput"')
    expect(projectDetailSource).toContain('type="file"')
    expect(projectDetailSource).toContain('multiple')
    expect(projectDetailSource).toContain('@click="openChatFilePicker"')
    expect(projectDetailSource).toContain('project-composer__attachments')
    expect(projectDetailSource).toContain('conversationStore.queueInitialFiles(conversation.id, files)')
    expect(chatWorkspaceSource).toContain('conversationStore.takeInitialFiles(conversationId)')
    expect(chatWorkspaceSource).toMatch(/await handleIncomingFiles\(initialFiles, 'picker'\)[\s\S]*await handleSend\(initialMessage\)/)
  })

  it('uses an explicitly interactive rename overlay and focuses its input', () => {
    expect(projectDetailSource).toContain('<Teleport to="body">')
    expect(projectDetailSource).toContain('ref="artifactRenameInput"')
    expect(projectDetailSource).toContain('@click.self="closeArtifactRename"')
    expect(projectDetailSource).toContain('artifactRenameInput.value?.select()')
    expect(projectDetailSource).not.toContain('<n-modal v-model:show="artifactRenameOpen"')
  })
})
