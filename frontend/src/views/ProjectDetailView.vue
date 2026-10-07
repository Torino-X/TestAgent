<template>
  <AppLayout>
    <section class="project-workspace" aria-labelledby="project-title">
      <div class="project-workspace__content">
        <div v-if="projectStore.loading.detail && !projectStore.activeProject" class="project-page-state" role="status">正在加载项目...</div>
        <div v-else-if="projectStore.errors.detail && !projectStore.activeProject" class="project-page-state project-page-state--error">
          <strong>项目加载失败</strong><span>{{ projectStore.errors.detail }}</span><button type="button" @click="loadProject">重试</button>
        </div>
        <template v-else>
          <header class="project-workspace__header">
            <div class="project-workspace__identity"><span class="project-workspace__folder"><n-icon :component="FolderOutline" :size="28" /></span><h1 id="project-title">{{ project.name }}</h1></div>
            <div class="project-workspace__actions"><button class="project-workspace__share" type="button" @click="showShareNotice"><n-icon :component="ShareSocialOutline" :size="17" />分享</button><button class="project-workspace__more" type="button" aria-label="项目设置" @click="settingsOpen = true"><n-icon :component="EllipsisHorizontal" :size="21" /></button></div>
          </header>

          <div class="project-composer" :class="{ 'project-composer--with-files': pendingChatFiles.length }">
            <div v-if="pendingChatFiles.length" class="project-composer__attachments" aria-label="本次对话的附件">
              <article v-for="file in pendingChatFiles" :key="chatFileKey(file)" class="project-composer__attachment">
                <span class="project-composer__attachment-icon" :class="`project-composer__attachment-icon--${chatFileIcon(file).kind}`">
                  <img v-if="chatFileIcon(file).src" :src="chatFileIcon(file).src" alt="" />
                  <n-icon v-else :component="DocumentTextOutline" :size="20" />
                </span>
                <span class="project-composer__attachment-copy"><strong :title="file.name">{{ file.name }}</strong><small>{{ chatFileLabel(file) }}</small></span>
                <button type="button" class="project-composer__attachment-remove" :aria-label="`移除 ${file.name}`" @click="removeChatFile(file)"><n-icon :component="CloseOutline" :size="15" /></button>
              </article>
            </div>
            <div class="project-composer__row">
              <button class="project-composer__source" type="button" aria-label="上传本次对话的文件" @click="openChatFilePicker"><n-icon :component="Add" :size="21" /></button>
              <input ref="chatFileInput" class="project-composer__file-input" type="file" multiple :disabled="startingChat" @change="selectChatFiles" />
              <input v-model="newChatText" type="text" placeholder="TestAgent中的新聊天" :disabled="startingChat" @keydown.enter.exact.prevent="startProjectChat" />
              <button class="project-composer__send" type="button" aria-label="发送消息" title="发送消息" :disabled="!newChatText.trim() || startingChat" @click="startProjectChat"><n-icon :component="ArrowUpOutline" :size="20" /></button>
            </div>
          </div>

          <div class="project-tabs-row">
            <nav class="project-tabs" aria-label="项目内容"><button v-for="item in tabs" :key="item.value" type="button" :class="{ active: tab === item.value }" @click="tab = item.value">{{ item.label }}</button></nav>
            <n-dropdown
              v-if="tab === 'sources'"
              :options="sourceSortOptions"
              trigger="click"
              placement="bottom-end"
              :show-arrow="false"
              :menu-props="sourceSortMenuProps"
              @select="selectSourceSort"
              @update:show="sourceSortMenuOpen = $event"
            >
              <button class="project-source-sort" :class="{ 'is-open': sourceSortMenuOpen }" type="button" aria-label="资料排序">
                {{ sourceSortLabel }}<n-icon :component="ChevronDownOutline" :size="13" />
              </button>
            </n-dropdown>
          </div>

          <section v-if="tab === 'chats'" class="project-panel" aria-label="项目聊天">
            <div v-if="project.conversations.length" class="project-conversations">
              <div v-for="conversation in project.conversations" :key="conversation.id" class="project-conversation">
                <button type="button" class="project-conversation__main" @click="openConversation(conversation.id)"><span><strong>{{ conversation.title }}</strong><small>{{ conversation.summary }}</small></span><time>{{ conversation.updatedAt }}</time></button>
                <button class="project-conversation__remove" type="button" :disabled="projectStore.loading.mutating" @click="removeConversation(conversation.id)">移出项目</button>
              </div>
            </div>
            <div v-else class="project-blank"><n-icon :component="ChatbubbleEllipsesOutline" :size="35" /><h2>从一个新聊天开始</h2><p>项目中的对话会保留各自的消息历史，并共享项目资料与设置。</p></div>
          </section>

          <section v-else-if="tab === 'sources'" class="project-panel project-panel--sources" :class="{ 'project-panel--sources-empty': !project.sources.length }" aria-label="项目来源">
            <div v-if="projectStore.errors.source" class="project-inline-error"><span>{{ projectStore.errors.source }}</span><button type="button" @click="refreshSources">重试</button></div>
            <template v-if="project.sources.length">
              <button class="project-source-add" type="button" @click="sourcePickerOpen = true">
                <span class="project-source-add__icon"><n-icon :component="Add" :size="20" /></span>
                <span>添加项目资料</span>
              </button>
              <article
                v-for="source in sortedSources"
                :key="source.id"
                class="project-source project-source--interactive"
                :class="{ 'project-source--disabled': !source.fileId }"
                :role="source.fileId ? 'button' : undefined"
                :tabindex="source.fileId ? 0 : undefined"
                @click="openSourcePreview(source)"
                @keydown.enter.space.prevent="openSourcePreview(source)"
              >
                <span class="project-source__main">
                  <span class="project-source__file-icon" :class="`project-source__file-icon--${sourceIcon(source.name).kind}`">
                    <img v-if="sourceIcon(source.name).src" :src="sourceIcon(source.name).src" alt="" />
                    <n-icon v-else :component="DocumentTextOutline" :size="19" />
                  </span>
                  <span class="project-source__identity"><em>文件</em><strong :title="source.name">{{ source.name }}</strong></span>
                </span>
                <time>{{ source.updatedAt }}</time>
                <button class="project-source__remove" type="button" @click.stop="removeSource(source.id)" @keydown.stop>移除</button>
              </article>
            </template>
            <div v-else-if="!projectStore.loading.source" class="project-blank project-blank--sources"><span class="project-blank__illustration"><span class="project-blank__source-icon project-blank__source-icon--slack">✦</span><span class="project-blank__source-icon project-blank__source-icon--drive">▲</span><span class="project-blank__source-icon"><n-icon :component="AttachOutline" :size="20" /></span></span><h2>为TestAgent提供更多背景信息</h2><p>上传项目资料，或从资料库关联已有文件，让项目中的对话获得持续、独立的背景信息。</p><button type="button" @click="sourcePickerOpen = true">添加来源</button></div>
            <div v-else class="project-page-state" role="status">正在加载项目来源...</div>
          </section>

          <section v-else class="project-panel" aria-label="测试资产">
            <div v-if="projectStore.errors.artifact" class="project-inline-error"><span>{{ projectStore.errors.artifact }}</span><button type="button" @click="refreshArtifacts">重试</button></div>
            <div v-if="project.artifacts.length" class="project-artifacts">
              <article v-for="artifact in project.artifacts" :key="artifact.id" class="project-artifact" role="button" tabindex="0" @click="openArtifactPreview(artifact)" @keydown.enter.space.prevent="openArtifactPreview(artifact)">
                <span class="project-artifact__main">
                  <span class="project-artifact__file-icon" :class="`project-artifact__file-icon--${artifactIcon(artifact.name).kind}`">
                    <img v-if="artifactIcon(artifact.name).src" :src="artifactIcon(artifact.name).src" alt="" />
                    <n-icon v-else :component="DocumentTextOutline" :size="19" />
                  </span>
                  <span class="project-artifact__identity"><em>{{ artifact.type }}</em><strong :title="artifact.name">{{ artifact.name }}</strong></span>
                </span>
                <time class="project-artifact__time">{{ artifact.updatedAt }}</time>
                <n-dropdown :options="artifactActionOptions" :menu-props="artifactActionMenuProps" trigger="click" placement="bottom-end" :show-arrow="false" @select="(key) => void handleArtifactAction(String(key), artifact)">
                  <button class="project-artifact__more" type="button" :aria-label="`${artifact.name}的更多操作`" @click.stop @keydown.stop><n-icon :component="EllipsisHorizontal" :size="20" /></button>
                </n-dropdown>
              </article>
            </div>
            <div v-else-if="!projectStore.loading.artifact" class="project-blank"><n-icon :component="DocumentTextOutline" :size="35" /><h2>尚无测试资产</h2><p>在此项目中完成的测试方案、测试用例和审查报告会显示在这里。</p></div>
            <div v-else class="project-page-state" role="status">正在加载测试资产...</div>
          </section>
        </template>
      </div>
    </section>
  </AppLayout>
  <ProjectSettingsDialog :show="settingsOpen" :project="projectStore.activeProject" :saving="projectStore.loading.mutating" @close="settingsOpen = false" @delete="requestDeleteProject" @save="saveSettings" />
  <ProjectSourcePicker :show="sourcePickerOpen" :loading="projectStore.loading.source" @close="sourcePickerOpen = false" @attach="attachLibrarySource" @upload="uploadSource" />
  <ConfirmDangerDialog :visible="deleteConfirmOpen" title="删除项目？" hint="项目中的聊天将变为独立聊天；资料库文件和已生成资产不会被删除。" confirm-label="删除项目" @cancel="deleteConfirmOpen = false" @confirm="deleteProject"><template #body>这会删除“<strong>{{ projectStore.activeProject?.name }}</strong>”。</template></ConfirmDangerDialog>
  <Teleport to="body">
    <div v-if="artifactRenameOpen" class="project-artifact-dialog-backdrop" @click.self="closeArtifactRename" @keydown.esc="closeArtifactRename">
      <section class="project-artifact-dialog" role="dialog" aria-modal="true" aria-labelledby="project-artifact-rename-title" @pointerdown.stop @click.stop>
        <h2 id="project-artifact-rename-title">重命名文件</h2>
        <input ref="artifactRenameInput" v-model="artifactRenameValue" type="text" aria-label="新文件名" :disabled="artifactMutating" @keydown.enter.prevent="submitArtifactRename" />
        <div class="project-artifact-dialog__actions"><button type="button" @click="closeArtifactRename">取消</button><button class="primary" type="button" :disabled="!artifactRenameValue.trim() || artifactMutating" @click="submitArtifactRename">重命名</button></div>
      </section>
    </div>
  </Teleport>
  <ConfirmDangerDialog :visible="artifactDeleteOpen" title="删除文件？" hint="删除后可在资料库的“最近删除”中恢复，30 天后永久删除。" confirm-label="删除" @cancel="closeArtifactDelete" @confirm="deleteArtifact"><template #body>这会将“<strong>{{ artifactTarget?.name }}</strong>”移至最近删除。</template></ConfirmDangerDialog>
</template>

<script setup lang="ts">
import { computed, h, nextTick, ref, watch, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NDropdown, NIcon, useMessage, type DropdownOption } from 'naive-ui'
import { Add, ArrowUpOutline, AttachOutline, ChatbubbleEllipsesOutline, CheckmarkOutline, ChevronDownOutline, CloseOutline, CreateOutline, DocumentTextOutline, DownloadOutline, EllipsisHorizontal, FolderOutline, ShareSocialOutline, TrashOutline } from '@vicons/ionicons5'
import AppLayout from '@/components/layout/AppLayout.vue'
import ConfirmDangerDialog from '@/components/common/ConfirmDangerDialog.vue'
import ProjectSettingsDialog from '@/components/projects/ProjectSettingsDialog.vue'
import ProjectSourcePicker from '@/components/projects/ProjectSourcePicker.vue'
import { useConversationStore } from '@/stores/conversationStore'
import { useProjectStore } from '@/stores/projectStore'
import { sortProjectSources, type ProjectSourceSort } from '@/utils/projectSourceSort'
import { downloadArtifact } from '@/api/artifactApi'
import { resolveFileIcon } from '@/utils/fileIcon'
import type { ProjectArtifactPreview } from '@/types/project'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const projectStore = useProjectStore()
const conversationStore = useConversationStore()
const tab = ref<'chats' | 'sources' | 'artifacts'>('chats')
const newChatText = ref('')
const chatFileInput = ref<HTMLInputElement | null>(null)
const pendingChatFiles = ref<File[]>([])
const settingsOpen = ref(false)
const sourcePickerOpen = ref(false)
const deleteConfirmOpen = ref(false)
const startingChat = ref(false)
const sourceSort = ref<ProjectSourceSort>('newest')
const sourceSortMenuOpen = ref(false)
const artifactRenameOpen = ref(false)
const artifactDeleteOpen = ref(false)
const artifactMutating = ref(false)
const artifactRenameValue = ref('')
const artifactRenameInput = ref<HTMLInputElement | null>(null)
const artifactTarget = ref<ProjectArtifactPreview | null>(null)
const tabs = [{ label: '聊天', value: 'chats' as const }, { label: '项目资料', value: 'sources' as const }, { label: '测试资产', value: 'artifacts' as const }]
const project = computed(() => projectStore.activeProject ?? { id: String(route.params.projectId), name: '正在加载项目', updatedAt: '刚刚', createdAt: '', description: '', createdByCurrentUser: true, memoryMode: 'project_memory' as const, pinned: false, instructions: '', conversations: [], sources: [], artifacts: [] })
const sortedSources = computed(() => sortProjectSources(project.value.sources, sourceSort.value))
const sourceSortLabels: Record<ProjectSourceSort, string> = { newest: '最新', oldest: '最早', alphabetical: '按字母顺序排列' }
const sourceSortLabel = computed(() => sourceSortLabels[sourceSort.value])
const sourceSortOptions = computed<DropdownOption[]>(() => (Object.entries(sourceSortLabels) as [ProjectSourceSort, string][]).map(([key, label]) => ({
  key,
  label: () => h('span', { class: 'project-source-sort-option' }, [
    h('span', label),
    sourceSort.value === key ? h(NIcon, { component: CheckmarkOutline, size: 17 }) : null
  ])
})))
const sourceSortMenuProps = () => ({ class: 'project-source-sort-menu' })
function iconOption(label: string, key: string, icon: typeof DownloadOutline, danger = false): DropdownOption {
  return {
    key,
    props: danger ? { class: 'project-artifact-action--danger' } : undefined,
    label: () => h('span', { class: ['project-artifact-action-option', danger ? 'project-artifact-action--danger' : ''] }, [
      h('span', { class: 'project-artifact-action-option__icon' }, [h(NIcon, { component: icon as Component, size: 18 })]),
      h('span', { class: 'project-artifact-action-option__label' }, label)
    ])
  }
}
const artifactActionOptions: DropdownOption[] = [
  iconOption('下载', 'download', DownloadOutline),
  iconOption('重命名', 'rename', CreateOutline),
  iconOption('删除', 'delete', TrashOutline, true)
]
const artifactActionMenuProps = () => ({ class: 'project-artifact-action-menu' })

watch(() => route.params.projectId, () => {
  pendingChatFiles.value = []
  newChatText.value = ''
  void loadProject()
}, { immediate: true })
watch(tab, (nextTab) => {
  if (nextTab === 'sources') void refreshSources()
  if (nextTab === 'artifacts') void refreshArtifacts()
})

function openConversation(id: string) { void router.push(`/chat/${encodeURIComponent(id)}`) }
function selectSourceSort(key: string | number) {
  if (key === 'newest' || key === 'oldest' || key === 'alphabetical') sourceSort.value = key
}
function artifactIcon(name: string) { return resolveFileIcon(name) }
function sourceIcon(name: string) { return resolveFileIcon(name) }
function chatFileKey(file: File) { return `${file.name}:${file.size}:${file.lastModified}:${file.type}` }
function chatFileIcon(file: File) { return resolveFileIcon(file.name) }
function chatFileLabel(file: File) {
  if (file.type.startsWith('image/')) return '图片'
  if (/\.(pdf|docx?|xlsx?|pptx?|txt|md|csv)$/i.test(file.name)) return '文档'
  return '文件'
}
function openChatFilePicker() { chatFileInput.value?.click() }
function selectChatFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const known = new Set(pendingChatFiles.value.map(chatFileKey))
  const additions = Array.from(input.files ?? []).filter((file) => {
    const key = chatFileKey(file)
    if ((!file.name && file.size === 0) || known.has(key)) return false
    known.add(key)
    return true
  })
  if (additions.length) pendingChatFiles.value = [...pendingChatFiles.value, ...additions]
  input.value = ''
}
function removeChatFile(file: File) {
  const key = chatFileKey(file)
  pendingChatFiles.value = pendingChatFiles.value.filter((candidate) => chatFileKey(candidate) !== key)
}
function openArtifactPreview(artifact: ProjectArtifactPreview) {
  void router.push({
    name: 'document-preview',
    params: { itemId: artifact.id },
    query: { from: 'project', projectId: project.value.id, projectName: project.value.name }
  })
}
function openSourcePreview(source: typeof project.value.sources[number]) {
  if (!source.fileId) return
  void router.push({
    name: 'document-preview',
    params: { itemId: source.fileId },
    query: { from: 'project', projectId: project.value.id, projectName: project.value.name }
  })
}
async function handleArtifactAction(action: string, artifact: ProjectArtifactPreview) {
  if (action === 'download') {
    try { await downloadArtifact(artifact.id, artifact.name) }
    catch (error) { message.error(error instanceof Error ? error.message : '下载失败，请稍后重试') }
    return
  }
  artifactTarget.value = artifact
  if (action === 'rename') {
    artifactRenameValue.value = artifact.name
    artifactRenameOpen.value = true
    void nextTick(() => {
      artifactRenameInput.value?.focus()
      artifactRenameInput.value?.select()
    })
  }
  if (action === 'delete') artifactDeleteOpen.value = true
}
function closeArtifactRename() { if (!artifactMutating.value) { artifactRenameOpen.value = false; artifactTarget.value = null } }
function closeArtifactDelete() { if (!artifactMutating.value) { artifactDeleteOpen.value = false; artifactTarget.value = null } }
async function submitArtifactRename() {
  const target = artifactTarget.value
  const name = artifactRenameValue.value.trim()
  if (!target || !name || artifactMutating.value) return
  artifactMutating.value = true
  try { await projectStore.renameProjectArtifact(target.id, name); artifactRenameOpen.value = false; artifactTarget.value = null; message.success('文件已重命名') }
  catch (error) { message.error(error instanceof Error ? error.message : '重命名失败') }
  finally { artifactMutating.value = false }
}
async function deleteArtifact() {
  const target = artifactTarget.value
  if (!target || artifactMutating.value) return
  artifactMutating.value = true
  try { await projectStore.deleteProjectArtifact(target.id); artifactDeleteOpen.value = false; artifactTarget.value = null; message.success('文件已移至最近删除') }
  catch (error) { message.error(error instanceof Error ? error.message : '删除失败') }
  finally { artifactMutating.value = false }
}
async function loadProject() { const projectId = route.params.projectId; if (typeof projectId !== 'string') return; try { await projectStore.fetchProject(projectId) } catch (error) { message.error(error instanceof Error ? error.message : '项目加载失败') } }
async function refreshSources() { try { await projectStore.fetchProjectSources() } catch (error) { message.error(error instanceof Error ? error.message : '项目来源加载失败') } }
async function refreshArtifacts() { try { await projectStore.fetchProjectArtifacts() } catch (error) { message.error(error instanceof Error ? error.message : '测试资产加载失败') } }
async function startProjectChat() {
  const text = newChatText.value.trim()
  if (!text || startingChat.value) return
  startingChat.value = true
  try {
    const activeProject = projectStore.activeProject
    const files = [...pendingChatFiles.value]
    if (!activeProject) throw new Error('项目尚未加载完成')
    const conversation = await projectStore.createProjectConversation(text.slice(0, 80))
    conversationStore.mergeConversationSummary({
      id: conversation.id,
      title: conversation.title,
      subtitle: conversation.summary,
      projectId: activeProject.id,
      projectName: activeProject.name,
      state: 'empty',
      updatedAt: conversation.updatedAt,
      messageCount: 0,
      fileCount: 0,
      latestTask: null,
      tasks: [],
      files: [],
      draftFiles: [],
      messages: []
    })
    conversationStore.queueInitialMessage(conversation.id, text)
    conversationStore.queueInitialFiles(conversation.id, files)
    newChatText.value = ''
    pendingChatFiles.value = []
    await router.push(`/chat/${encodeURIComponent(conversation.id)}`)
  } catch (error) { message.error(error instanceof Error ? error.message : '创建项目聊天失败') }
  finally { startingChat.value = false }
}
function showShareNotice() { message.info('当前版本暂未开放项目协作分享') }
async function saveSettings(payload: { name: string; description: string; memoryMode: 'project_memory' | 'none'; instructions: string }) {
  try { await projectStore.updateActiveProject(payload); settingsOpen.value = false; message.success('项目设置已保存') } catch (error) { message.error(error instanceof Error ? error.message : '保存项目设置失败') }
}
function requestDeleteProject() { deleteConfirmOpen.value = true }
async function deleteProject() {
  const activeProject = projectStore.activeProject
  if (!activeProject) return
  try { await projectStore.deleteProject(activeProject.id); deleteConfirmOpen.value = false; settingsOpen.value = false; message.success('项目已删除'); await router.push('/projects') } catch (error) { message.error(error instanceof Error ? error.message : '删除项目失败') }
}
async function uploadSource(file: File) { try { await projectStore.uploadSource(file); sourcePickerOpen.value = false; message.success('资料已上传并关联到项目') } catch (error) { message.error(error instanceof Error ? error.message : '添加来源失败') } }
async function attachLibrarySource(fileId: string) { try { await projectStore.addLibrarySource({ fileId }); sourcePickerOpen.value = false; message.success('资料库文件已关联到项目') } catch (error) { message.error(error instanceof Error ? error.message : '关联资料库文件失败') } }
async function removeSource(sourceId: string) { try { await projectStore.removeSource(sourceId); message.success('资料已从项目移除') } catch (error) { message.error(error instanceof Error ? error.message : '移除来源失败') } }
async function removeConversation(conversationId: string) { try { await projectStore.removeProjectConversation(conversationId); conversationStore.setConversationProject(conversationId, null); message.success('聊天已移出项目') } catch (error) { message.error(error instanceof Error ? error.message : '移出项目失败') } }
</script>

<style scoped>
.project-workspace { flex: 1; min-width: 0; height: 100%; min-height: 100vh; overflow-y: auto; color: #171717; background: #fff; }.project-workspace__content { width: min(770px, calc(100% - 64px)); min-height: 100%; margin: 0 auto; padding: 116px 0 86px; }.project-workspace__header { display: flex; align-items: center; justify-content: space-between; }.project-workspace__identity { display: flex; gap: 8px; align-items: center; min-width: 0; }.project-workspace__folder { display: grid; flex: 0 0 auto; width: 34px; height: 34px; color: #191919; place-items: center; }.project-workspace h1 { margin: 0; overflow: hidden; font-size: 29px; font-weight: 500; letter-spacing: -.65px; text-overflow: ellipsis; white-space: nowrap; }.project-workspace__actions { display: flex; gap: 7px; align-items: center; }.project-workspace__actions button { display: inline-flex; height: 37px; gap: 6px; align-items: center; justify-content: center; padding: 0 14px; color: #222; font: inherit; font-size: 14px; cursor: pointer; background: #fff; border: 1px solid #dedede; border-radius: 999px; transition: background .16s ease, transform .16s ease; }.project-workspace__actions button:hover { background: #f6f6f6; }.project-workspace__actions button:active { transform: scale(.98); }.project-workspace__actions .project-workspace__more { display: grid; width: 37px; padding: 0; place-items: center; }
.project-composer { display: flex; height: 54px; gap: 9px; align-items: center; margin-top: 21px; padding: 0 11px; background: #fff; border: 1px solid #e5e5e5; border-radius: 28px; box-shadow: 0 10px 26px rgba(20, 20, 20, .065); }.project-composer:focus-within { border-color: #bdbdbd; }.project-composer button { display: grid; flex: 0 0 auto; width: 32px; height: 32px; padding: 0; color: #303030; cursor: pointer; background: transparent; border: 0; border-radius: 50%; place-items: center; transition: background .16s ease, transform .16s ease; }.project-composer button:hover { background: #f2f2f2; }.project-composer button:active { transform: scale(.96); }.project-composer input { flex: 1; min-width: 0; color: #222; font: inherit; font-size: 16px; outline: none; border: 0; }.project-composer input::placeholder { color: #929292; }
.project-composer .project-composer__send { width: 36px; height: 36px; color: #fff; background: #3f83f8; }.project-composer .project-composer__send:hover:not(:disabled) { background: #2f76eb; }.project-composer .project-composer__send:disabled { color: #fff; cursor: default; background: #d7d7d7; }.project-composer .project-composer__send:disabled:active { transform: none; }
.project-composer { box-sizing: border-box; height: auto; min-height: 54px; flex-direction: column; gap: 0; padding: 6px 10px; }
.project-composer__row { display: flex; width: 100%; min-height: 40px; gap: 9px; align-items: center; }
.project-composer .project-composer__file-input { display: none; }
.project-composer__attachments { display: grid; width: 100%; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 2px 0 6px; }
.project-composer__attachment { position: relative; display: flex; min-width: 0; height: 58px; gap: 10px; align-items: center; padding: 0 34px 0 10px; background: #fff; border: 1px solid #e3e3e3; border-radius: 16px; }
.project-composer__attachment-icon { display: grid; flex: 0 0 auto; width: 31px; height: 31px; overflow: hidden; color: #1683ff; background: #f8f8f8; border: 1px solid #ececec; border-radius: 8px; place-items: center; }
.project-composer__attachment-icon img { width: 19px; height: 19px; object-fit: contain; }
.project-composer__attachment-icon--word img { filter: invert(43%) sepia(96%) saturate(2663%) hue-rotate(199deg) brightness(102%) contrast(101%); }
.project-composer__attachment-icon--excel img { filter: invert(52%) sepia(82%) saturate(456%) hue-rotate(92deg) brightness(83%) contrast(91%); }
.project-composer__attachment-icon--pdf img { filter: invert(27%) sepia(92%) saturate(2733%) hue-rotate(345deg) brightness(101%) contrast(91%); }
.project-composer__attachment-icon--markdown img { filter: invert(40%) sepia(55%) saturate(909%) hue-rotate(210deg) brightness(88%) contrast(91%); }
.project-composer__attachment-copy { display: grid; min-width: 0; gap: 2px; text-align: left; }
.project-composer__attachment-copy strong { overflow: hidden; color: #202020; font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.project-composer__attachment-copy small { color: #777; font-size: 12px; }
.project-composer .project-composer__attachment-remove { position: absolute; top: 5px; right: 5px; width: 24px; height: 24px; color: #777; opacity: 0; }
.project-composer__attachment:hover .project-composer__attachment-remove, .project-composer__attachment:focus-within .project-composer__attachment-remove { opacity: 1; }
.project-tabs-row { display: flex; align-items: center; justify-content: space-between; margin-top: 29px; }.project-tabs { display: flex; gap: 5px; }.project-tabs button { height: 41px; padding: 0 16px; color: #6f6f6f; font: inherit; font-size: 14px; cursor: pointer; background: transparent; border: 0; border-radius: 999px; transition: color .16s ease, background .16s ease; }.project-tabs button:hover { color: #242424; background: #f5f5f5; }.project-tabs button.active { color: #171717; font-weight: 500; background: #f0f0f0; }.project-source-sort { display: inline-flex; height: 34px; gap: 4px; align-items: center; padding: 0 12px; color: #555; font: inherit; font-size: 14px; cursor: pointer; background: transparent; border: 0; border-radius: 999px; transition: color .16s ease, background .16s ease; }.project-source-sort:hover, .project-source-sort.is-open { color: #171717; background: #f0f0f0; }
.project-panel { margin-top: 15px; }.project-conversations { display: grid; }.project-conversation { display: flex; min-height: 65px; align-items: center; padding: 0 8px 0 12px; border-bottom: 1px solid #eee; border-radius: 10px; transition: background .16s ease; }.project-conversation:hover { background: #f7f7f7; }.project-conversation__main { display: flex; flex: 1; min-width: 0; min-height: 64px; gap: 24px; align-items: center; justify-content: space-between; padding: 0; color: #171717; font: inherit; text-align: left; cursor: pointer; background: transparent; border: 0; }.project-conversation__main span { display: grid; min-width: 0; gap: 3px; }.project-conversation strong { overflow: hidden; font-size: 14px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }.project-conversation small { overflow: hidden; color: #5d5d5d; font-size: 14px; line-height: 18px; text-overflow: ellipsis; white-space: nowrap; }.project-conversation time, .project-source time, .project-artifact time { flex: 0 0 auto; color: #747474; font-size: 14px; }.project-conversation__remove { flex: 0 0 auto; margin-left: 12px; padding: 5px 8px; color: #777; font: inherit; font-size: 12px; cursor: pointer; background: transparent; border: 0; border-radius: 6px; opacity: 0; }.project-conversation:hover .project-conversation__remove, .project-conversation__remove:focus-visible { opacity: 1; }.project-conversation__remove:hover { color: #222; background: #ebebeb; }
.project-source-add { display: flex; width: 100%; min-height: 64px; gap: 12px; align-items: center; margin-bottom: 10px; padding: 0 13px; color: #202020; font: inherit; font-size: 14px; font-weight: 500; text-align: left; cursor: pointer; background: transparent; border: 0; border-radius: 11px; transition: background .16s ease, transform .16s ease; }.project-source-add:hover, .project-source-add:focus-visible { background: #f2f2f1; outline: none; }.project-source-add:active { transform: scale(.997); }.project-source-add__icon { display: grid; width: 34px; height: 34px; color: #555; background: transparent; border-radius: 50%; place-items: center; }.project-source-add:hover .project-source-add__icon, .project-source-add:focus-visible .project-source-add__icon { background: #e4e4e2; }
.project-blank { display: flex; min-height: 320px; flex-direction: column; align-items: center; justify-content: center; color: #666; text-align: center; }.project-blank h2 { margin: 17px 0 7px; color: #171717; font-size: 17px; font-weight: 500; }.project-blank p { max-width: 440px; margin: 0; font-size: 14px; line-height: 21px; }.project-blank button { height: 31px; margin-top: 21px; padding: 0 12px; color: #fff; font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; background: #171717; border: 0; border-radius: 999px; }.project-panel--sources-empty { box-sizing: border-box; min-height: 322px; border: 1px dashed #dedede; border-radius: 17px; }.project-blank--sources { min-height: 320px; padding: 0 28px; }.project-blank__illustration { display: flex; align-items: center; margin-bottom: 8px; }.project-blank__source-icon { display: grid; width: 37px; height: 37px; margin-left: -5px; color: #707070; background: #fff; border: 1px solid #e5e5e5; border-radius: 11px; box-shadow: 0 4px 11px rgba(20, 20, 20, .06); place-items: center; }.project-blank__source-icon:first-child { margin-left: 0; }.project-blank__source-icon--slack { color: #e54679; font-size: 21px; }.project-blank__source-icon--drive { color: #55ad62; font-size: 16px; }
.project-source { display: grid; grid-template-columns: minmax(0, 1fr) 110px auto; min-height: 70px; gap: 14px; align-items: center; padding: 0 8px 0 12px; cursor: pointer; border-bottom: 1px solid #eee; border-radius: 11px; outline: none; transition: background .15s ease, box-shadow .15s ease; }.project-source:last-child { border-bottom-color: transparent; }.project-source:hover, .project-source:focus-within { background: #f5f5f5; }.project-source:focus-visible { box-shadow: inset 0 0 0 2px #b8d3ff; }.project-source--disabled { cursor: default; }.project-source__main { display: flex; min-width: 0; gap: 12px; align-items: center; }.project-source__identity { display: grid; min-width: 0; gap: 4px; }.project-source__identity em { color: #757575; font-size: 12px; font-style: normal; }.project-source__file-icon { display: grid; flex: 0 0 auto; width: 33px; height: 33px; overflow: hidden; color: #1683ff; background: #fff; border: 1px solid #e6e6e6; border-radius: 9px; place-items: center; }.project-source__file-icon img { width: 20px; height: 20px; object-fit: contain; }.project-source__file-icon--word img { filter: invert(43%) sepia(96%) saturate(2663%) hue-rotate(199deg) brightness(102%) contrast(101%); }.project-source__file-icon--excel img { filter: invert(52%) sepia(82%) saturate(456%) hue-rotate(92deg) brightness(83%) contrast(91%); }.project-source__file-icon--pdf img { filter: invert(27%) sepia(92%) saturate(2733%) hue-rotate(345deg) brightness(101%) contrast(91%); }.project-source__file-icon--markdown img { filter: invert(40%) sepia(55%) saturate(909%) hue-rotate(210deg) brightness(88%) contrast(91%); }.project-source strong, .project-artifact strong { overflow: hidden; font-size: 14px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }.project-source time { justify-self: start; }.project-source__remove { padding: 5px 8px; color: #6a6a6a; font: inherit; font-size: 12px; cursor: pointer; background: transparent; border: 0; border-radius: 6px; opacity: 0; transition: opacity .15s ease, color .15s ease, background .15s ease; }.project-source:hover .project-source__remove, .project-source__remove:focus-visible { opacity: 1; }.project-source__remove:hover { color: #171717; background: #e8e8e8; }
.project-artifact { display: grid; grid-template-columns: minmax(0, 1fr) 150px 36px; gap: 14px; align-items: center; min-height: 70px; padding: 0 8px 0 12px; cursor: pointer; border-bottom: 1px solid #eee; border-radius: 11px; outline: none; transition: background .15s ease; }.project-artifact:last-child { border-bottom-color: transparent; }.project-artifact:hover, .project-artifact:focus-within { background: #f5f5f5; }.project-artifact:focus-visible { box-shadow: inset 0 0 0 2px #b8d3ff; }.project-artifact__main { display: flex; min-width: 0; gap: 12px; align-items: center; }.project-artifact__identity { display: grid; min-width: 0; gap: 4px; }.project-artifact__file-icon { display: grid; flex: 0 0 auto; width: 33px; height: 33px; overflow: hidden; color: #1683ff; background: #fff; border: 1px solid #e6e6e6; border-radius: 9px; place-items: center; }.project-artifact__file-icon img { width: 20px; height: 20px; object-fit: contain; }.project-artifact__file-icon--word img { filter: invert(43%) sepia(96%) saturate(2663%) hue-rotate(199deg) brightness(102%) contrast(101%); }.project-artifact__file-icon--excel img { filter: invert(52%) sepia(82%) saturate(456%) hue-rotate(92deg) brightness(83%) contrast(91%); }.project-artifact__file-icon--pdf img { filter: invert(27%) sepia(92%) saturate(2733%) hue-rotate(345deg) brightness(101%) contrast(91%); }.project-artifact__file-icon--markdown img { filter: invert(40%) sepia(55%) saturate(909%) hue-rotate(210deg) brightness(88%) contrast(91%); }.project-artifact em { color: #757575; font-size: 12px; font-style: normal; }.project-artifact__time { justify-self: start; color: #747474; font-size: 14px; }.project-artifact__more { display: grid; width: 36px; height: 36px; padding: 0; color: #8a8a8a; cursor: pointer; background: transparent; border: 0; border-radius: 9px; place-items: center; }.project-artifact__more:hover, .project-artifact__more:focus-visible { color: #222; background: #e8e8e8; }
.project-artifact-dialog { box-sizing: border-box; width: min(450px, calc(100vw - 40px)); padding: 22px 18px 16px; color: #171717; background: #fff; border-radius: 16px; box-shadow: 0 18px 55px rgba(15,23,42,.2); }.project-artifact-dialog h2 { margin: 0 0 16px; font-size: 18px; font-weight: 600; }.project-artifact-dialog input { box-sizing: border-box; width: 100%; height: 42px; padding: 0 12px; color: #171717; font: inherit; font-size: 14px; border: 1px solid #bdbdbd; border-radius: 8px; outline: none; }.project-artifact-dialog input:focus { border-color: #111; box-shadow: 0 0 0 1px #111; }.project-artifact-dialog__actions { display: flex; gap: 9px; justify-content: flex-end; margin-top: 16px; }.project-artifact-dialog__actions button { min-width: 62px; height: 36px; padding: 0 14px; color: #171717; font: inherit; cursor: pointer; background: #fff; border: 1px solid #dedede; border-radius: 999px; }.project-artifact-dialog__actions .primary { color: #fff; background: #171717; border-color: #171717; }.project-artifact-dialog__actions button:disabled { cursor: default; opacity: .5; }
.project-artifact-dialog-backdrop { position: fixed; inset: 0; z-index: 1000; display: grid; padding: 24px; background: rgba(0, 0, 0, .34); backdrop-filter: blur(2px); place-items: center; }
.project-artifact-dialog { pointer-events: auto; }
.project-page-state { display: flex; min-height: 320px; flex-direction: column; gap: 10px; align-items: center; justify-content: center; color: #777; font-size: 14px; text-align: center; }.project-page-state--error strong { color: #1d1d1d; font-size: 17px; }.project-page-state button, .project-inline-error button { padding: 6px 11px; color: #222; font: inherit; cursor: pointer; background: #fff; border: 1px solid #d8d8d8; border-radius: 999px; }.project-inline-error { display: flex; min-height: 45px; gap: 10px; align-items: center; justify-content: center; color: #b42318; font-size: 13px; background: #fff6f5; border-radius: 10px; }
@media (max-width: 860px) { .project-workspace__content { width: min(100% - 32px, 770px); padding-top: 35px; }.project-workspace h1 { font-size: 23px; }.project-workspace__share { display: none !important; }.project-composer { margin-top: 27px; }.project-composer__attachments { grid-template-columns: minmax(0, 1fr); }.project-tabs-row { margin-top: 25px; }.project-conversation { padding-right: 5px; }.project-conversation time { font-size: 12px; }.project-conversation__remove { opacity: 1; }.project-source { grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; }.project-source__remove { opacity: 1; }.project-artifact { grid-template-columns: minmax(0, 1fr) auto 36px; gap: 8px; }.project-artifact__time { font-size: 12px; } }
</style>

<style>
.project-source-sort-menu { width: 180px; padding: 8px !important; border-radius: 18px !important; box-shadow: 0 14px 38px rgba(20, 20, 20, .14) !important; }
.project-source-sort-menu .n-dropdown-option { min-height: 36px; border-radius: 9px; }
.project-source-sort-menu .n-dropdown-option-body__label { width: 100%; }
.project-source-sort-option { display: flex; width: 100%; gap: 18px; align-items: center; justify-content: space-between; }
.project-artifact-action-menu { width: 160px; padding: 8px !important; border-radius: 16px !important; box-shadow: 0 14px 38px rgba(20, 20, 20, .14) !important; }
.project-artifact-action-menu .n-dropdown-option { min-height: 38px; border-radius: 9px; }
.project-artifact-action-menu .n-dropdown-option-body__prefix { display: none; }
.project-artifact-action-menu .n-dropdown-option-body__label { width: 100%; }
.project-artifact-action-option { display: flex; width: 100%; gap: 10px; align-items: center; }
.project-artifact-action-option__icon { display: grid; flex: 0 0 18px; width: 18px; height: 18px; place-items: center; }
.project-artifact-action-option__label { min-width: 0; line-height: 20px; }
.project-artifact-action-menu .n-dropdown-option.project-artifact-action--danger, .project-artifact-action-menu .project-artifact-action--danger .n-dropdown-option-body, .project-artifact-action-menu .project-artifact-action--danger .n-dropdown-option-body__label, .project-artifact-action-menu .project-artifact-action--danger .n-dropdown-option-body__prefix { color: #e4202b !important; }
</style>
