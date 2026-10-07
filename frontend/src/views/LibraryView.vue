<template>
  <AppLayout>
    <section class="library-page" aria-labelledby="library-title">
      <div class="library-content">
        <header class="library-header">
          <h1 id="library-title">资料库</h1>
          <div class="library-header__actions">
            <n-input
              v-model:value="query"
              class="library-search"
              clearable
              placeholder="搜索"
              aria-label="搜索资料库"
              @update:value="scheduleLoad"
            >
              <template #prefix><n-icon :component="SearchOutline" /></template>
            </n-input>
            <button class="library-new" type="button" :disabled="isUploading" @click="openLibraryUpload">上传文件</button>
          </div>
        </header>
        <input ref="libraryUploadInput" class="library-upload-input" type="file" multiple @change="handleLibraryUpload" />

        <div class="library-toolbar" :class="{ 'library-toolbar--selection': hasSelection }">
          <nav v-if="!hasSelection" class="library-tabs" aria-label="资料类型">
            <button
              v-for="tab in tabs"
              :key="tab.value"
              class="library-tab"
              :class="{ 'library-tab--active': category === tab.value }"
              type="button"
              @click="selectCategory(tab.value)"
            >{{ tab.label }}</button>
          </nav>
          <div v-else class="library-bulk-actions" aria-label="已选文件操作">
            <button class="library-bulk-actions__chat" type="button" :disabled="isStartingChat" @click="void startChatWithItems(selectedItems)"><n-icon :component="ChatbubblesOutline" /><span>{{ isStartingChat ? '正在创建…' : '开始新的聊天' }}</span></button>
            <n-button round size="small" @click="downloadSelected"><template #icon><n-icon :component="DownloadOutline" /></template>下载</n-button>
            <n-button class="library-bulk-actions__delete" round size="small" @click="requestDelete(selectedItems)"><template #icon><n-icon :component="TrashOutline" /></template>删除</n-button>
          </div>
          <div class="library-view-actions" aria-label="视图设置">
            <n-popover v-if="!hasSelection" v-model:show="filterOpen" trigger="click" placement="bottom-end" :show-arrow="false">
              <template #trigger>
                <button
                  class="library-icon-button"
                  :class="{ 'library-icon-button--active': hasActiveFilters }"
                  type="button"
                  aria-label="筛选资料"
                  title="筛选"
                >
                  <n-icon :component="FunnelOutline" :size="19" />
                  <span v-if="activeFilterCount" class="library-filter-badge">{{ activeFilterCount }}</span>
                </button>
              </template>
              <div class="library-filter-menu">
                <p class="library-filter-menu__label">来源</p>
                <button class="library-filter-option" :class="{ active: source === 'upload' }" @click="setSource('upload')">
                  <span class="library-filter-option__content"><n-icon :component="CloudUploadOutline" />用户上传</span>
                  <img v-if="source === 'upload'" class="library-filter-option__check" :src="checkmarkIcon" alt="" aria-hidden="true" />
                </button>
                <button class="library-filter-option" :class="{ active: source === 'generated' }" @click="setSource('generated')">
                  <span class="library-filter-option__content"><n-icon :component="ColorWandOutline" />系统生成</span>
                  <img v-if="source === 'generated'" class="library-filter-option__check" :src="checkmarkIcon" alt="" aria-hidden="true" />
                </button>
                <div class="library-filter-divider" />
                <p class="library-filter-menu__label">文件类型</p>
                <button v-for="option in fileTypeOptions" :key="option.value" class="library-filter-option" :class="{ active: fileType === option.value }" @click="setFileType(option.value)">
                  <span class="library-filter-option__content"><n-icon :component="option.icon" />{{ option.label }}</span>
                  <img v-if="fileType === option.value" class="library-filter-option__check" :src="checkmarkIcon" alt="" aria-hidden="true" />
                </button>
                <div class="library-filter-divider" />
                <button class="library-filter-option" @click="resetFileFilters"><n-icon :component="EyeOutline" />显示全部文件类型</button>
                <button class="library-filter-option" :class="{ active: scope === 'deleted' }" @click="showDeleted"><span class="library-filter-option__content"><n-icon :component="TrashOutline" />最近删除</span><img v-if="scope === 'deleted'" class="library-filter-option__check" :src="checkmarkIcon" alt="" aria-hidden="true" /></button>
              </div>
            </n-popover>
            <span v-if="!hasSelection" class="library-toolbar__divider" aria-hidden="true" />
            <span v-if="hasSelection" class="library-selection-count">已选 {{ selectedItems.length }} 个</span>
            <button class="library-icon-button" :class="{ 'library-icon-button--active': viewMode === 'grid' }" type="button" aria-label="网格视图" title="网格视图" @click="viewMode = 'grid'">
              <n-icon :component="GridOutline" :size="19" />
            </button>
            <button class="library-icon-button" :class="{ 'library-icon-button--active': viewMode === 'list' }" type="button" aria-label="列表视图" title="列表视图" @click="viewMode = 'list'">
              <n-icon :component="ListOutline" :size="19" />
            </button>
          </div>
        </div>

        <div v-if="scope === 'deleted'" class="library-recycle-notice">
          <span>恢复你最近删除的文件。文件将在 30 天后被永久删除。</span>
          <button type="button" @click="backToLibrary">返回资料库</button>
        </div>

        <div v-if="isLoading" class="library-loading" aria-label="正在加载资料库">
          <n-skeleton v-for="index in 5" :key="index" text :repeat="1" />
        </div>
        <template v-else-if="items.length">
          <div v-if="viewMode === 'list'" class="library-list" role="list">
            <div class="library-list__head" :class="{ 'library-list__head--selecting': hasSelection }" aria-hidden="true">
              <button class="library-list__select-all" type="button" tabindex="-1" @click.stop="toggleSelectAll"><n-icon :component="allVisibleSelected ? Checkmark : RemoveOutline" :size="14" /></button>
              <span>名称</span><span>{{ scope === 'deleted' ? '删除时间' : '修改时间' }}</span><span>大小</span>
            </div>
            <div v-for="item in items" :key="item.id" class="library-row-item" :class="{ 'library-row-item--selected': isSelected(item.id) }" role="listitem" :aria-label="`资料：${item.name}`">
              <button class="library-row__selection" :class="{ 'library-row__selection--selected': isSelected(item.id) }" type="button" :aria-label="`选择 ${item.name}`" :aria-pressed="isSelected(item.id)" @click.stop="toggleSelection(item.id)"><n-icon v-if="isSelected(item.id)" :component="Checkmark" :size="14" /></button>
              <div class="library-row" :class="{ 'library-row--selected': isSelected(item.id) }" @click="selectItem(item)">
                <span class="library-row__name">
                <span class="library-file-icon" :class="`library-file-icon--${item.kind}`">
                  <img v-if="item.kind === 'image' && item.thumbnailUrl" :src="item.thumbnailUrl" alt="" />
                  <img v-else-if="libraryFileIconSrc(item)" class="library-file-icon__asset" :src="libraryFileIconSrc(item)" alt="" />
                  <n-icon v-else :component="item.kind === 'image' ? ImageOutline : DocumentTextOutline" :size="18" />
                </span>
                <span class="library-file-name">{{ item.name }}</span>
                <span v-if="item.source === 'generated'" class="library-source">生成</span>
              </span>
              <span class="library-row__date">{{ formatModifiedAt(scope === 'deleted' ? item.deletedAt || item.modifiedAt : item.modifiedAt) }}</span>
              <span class="library-row__size">{{ formatSize(item.sizeBytes) }}</span>
              <span class="library-row__menu">
                <n-dropdown :options="rowActions(scope)" :menu-props="libraryRowMenuProps" trigger="click" placement="bottom-end" @select="(key) => void handleItemAction(String(key), item)">
                  <button type="button" class="library-row__more" aria-label="更多操作" @click.stop><n-icon :component="EllipsisHorizontal" :size="20" /></button>
                </n-dropdown>
              </span>
              </div>
            </div>
          </div>
          <div v-else class="library-grid" role="list">
            <button v-for="item in items" :key="item.id" class="library-grid-card" type="button" role="listitem" @click="selectItem(item)">
              <span class="library-grid-card__preview" :class="`library-grid-card__preview--${item.kind}`">
                <img v-if="item.kind === 'image' && item.thumbnailUrl" :src="item.thumbnailUrl" alt="" />
                <img v-else-if="libraryFileIconSrc(item)" class="library-grid-file-icon__asset" :src="libraryFileIconSrc(item)" alt="" />
                <n-icon v-else :component="item.kind === 'image' ? ImageOutline : DocumentTextOutline" :size="28" />
              </span>
              <span class="library-grid-card__name">{{ item.name }}</span>
              <span class="library-grid-card__meta">{{ formatModifiedAt(item.modifiedAt) }} · {{ formatSize(item.sizeBytes) }}</span>
            </button>
          </div>
          <div v-if="hasMore" class="library-load-more-wrap">
            <button class="library-load-more" type="button" :disabled="isLoadingMore" @click="loadMore">
              {{ isLoadingMore ? '加载中…' : `加载更多（已显示 ${items.length} / ${total}）` }}
            </button>
          </div>
        </template>
        <div v-else class="library-empty" :class="{ 'library-empty--deleted': scope === 'deleted' }">
          <n-icon :component="SearchOutline" :size="34" /><span>{{ scope === 'deleted' ? '未找到文件' : '没有找到资料' }}</span>
        </div>
      </div>
    </section>
  </AppLayout>

  <Teleport to="body">
    <div v-if="renameVisible" class="library-dialog-backdrop" @click.self="closeRename" @keydown.esc="closeRename">
      <section class="library-rename-dialog" role="dialog" aria-modal="true" aria-labelledby="library-rename-title" @pointerdown.stop @click.stop>
        <h2 id="library-rename-title">重命名文件</h2>
        <input ref="renameInput" v-model="renameValue" class="library-rename-dialog__input" type="text" aria-label="新文件名" :disabled="isRenaming" @keydown.enter.prevent="submitRename" />
        <div class="library-rename-dialog__actions"><button class="library-rename-dialog__cancel" type="button" :disabled="isRenaming" @click="closeRename">取消</button><button class="library-rename-dialog__submit" type="button" :disabled="isRenaming || !renameValue.trim()" @click="submitRename">重命名</button></div>
      </section>
    </div>
    <div v-if="deleteConfirmVisible" class="library-dialog-backdrop" @click.self="closeDeleteConfirm" @keydown.esc="closeDeleteConfirm">
      <section class="library-delete-modal" role="dialog" aria-modal="true" aria-labelledby="library-delete-title" @pointerdown.stop @click.stop>
        <h2 id="library-delete-title">删除文件？</h2>
        <p>{{ deleteCandidates.length === 1 ? `“${deleteCandidates[0]?.name}”将被移至“最近删除”，并在 30 天后永久删除。` : `所选 ${deleteCandidates.length} 个文件将被移至“最近删除”，并在 30 天后永久删除。` }}</p>
        <div class="library-delete-modal__actions"><button class="library-delete-modal__cancel" type="button" :disabled="isDeleting" @click="closeDeleteConfirm">取消</button><button class="library-delete-modal__submit" type="button" :disabled="isDeleting" @click="confirmDelete">{{ isDeleting ? '删除中…' : '删除' }}</button></div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NDropdown, NIcon, NInput, NPopover, NSkeleton, useMessage, type DropdownOption } from 'naive-ui'
import {
  ChatbubblesOutline, Checkmark, CloudUploadOutline, ColorWandOutline, CreateOutline, DocumentTextOutline,
  DownloadOutline, EllipsisHorizontal, EyeOutline, FunnelOutline, GridOutline, ImageOutline,
  ListOutline, RemoveOutline, SearchOutline, TrashOutline, RefreshOutline, DocumentOutline, GridOutline as SpreadsheetOutline
} from '@vicons/ionicons5'
import AppLayout from '@/components/layout/AppLayout.vue'
import checkmarkIcon from '@/assets/checkmark.svg'
import {
  deleteLibraryItem, downloadLibraryItem, fetchLibraryItemBlob, fetchLibraryItems, permanentlyDeleteLibraryItem,
  uploadLibraryFiles,
  renameLibraryItem, restoreLibraryItem
} from '@/api/libraryApi'
import { getMockLibraryItems } from '@/mocks/library'
import type { LibraryCategory, LibraryFileType, LibraryItem, LibraryScope, LibrarySourceFilter } from '@/types/library'
import { useConversationStore } from '@/stores/conversationStore'
import { useFileStore } from '@/stores/fileStore'
import type { FileType } from '@/types'
import { resolveColoredFileIcon } from '@/utils/fileIcon'

const router = useRouter()
const message = useMessage()
const conversationStore = useConversationStore()
const fileStore = useFileStore()
const category = ref<LibraryCategory>('all')
const query = ref('')
const scope = ref<LibraryScope>('active')
const source = ref<LibrarySourceFilter>('all')
const fileType = ref<LibraryFileType>('all')
const viewMode = ref<'list' | 'grid'>('list')
const items = ref<LibraryItem[]>([])
const isLoading = ref(true)
const isLoadingMore = ref(false)
const total = ref(0)
const currentPage = ref(1)
const hasMore = ref(false)
const libraryPageSize = 50
const filterOpen = ref(false)
const renameVisible = ref(false)
const renameValue = ref('')
const renameTarget = ref<LibraryItem | null>(null)
const renameInput = ref<HTMLInputElement | null>(null)
const isRenaming = ref(false)
const selectedIds = ref<string[]>([])
const isStartingChat = ref(false)
const deleteConfirmVisible = ref(false)
const deleteCandidates = ref<LibraryItem[]>([])
const isDeleting = ref(false)
const isUploading = ref(false)
const libraryUploadInput = ref<HTMLInputElement | null>(null)
let searchTimer: ReturnType<typeof setTimeout> | undefined

const tabs: Array<{ label: string; value: LibraryCategory }> = [
  { label: '全部', value: 'all' }, { label: '图片', value: 'image' }, { label: '文件', value: 'file' }
]
const fileTypeOptions: Array<{ label: string; value: LibraryFileType; icon: typeof ImageOutline }> = [
  { label: '图片', value: 'image', icon: ImageOutline },
  { label: '文档', value: 'document', icon: DocumentOutline },
  { label: '电子表格', value: 'spreadsheet', icon: SpreadsheetOutline },
  { label: '演示文稿', value: 'presentation', icon: DocumentTextOutline },
  { label: 'PDF', value: 'pdf', icon: DocumentTextOutline }
]
const activeFilterCount = computed(() => Number(scope.value === 'deleted') + Number(source.value !== 'all') + Number(fileType.value !== 'all'))
const hasActiveFilters = computed(() => activeFilterCount.value > 0)
const selectedItems = computed(() => items.value.filter((item) => selectedIds.value.includes(item.id)))
const hasSelection = computed(() => selectedItems.value.length > 0)
const allVisibleSelected = computed(() => items.value.length > 0 && items.value.every((item) => selectedIds.value.includes(item.id)))
const libraryRowMenuProps = () => ({ class: 'library-row-action-menu' })

function formatSize(size?: number | null) {
  if (!size) return '—'
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(size / 1024))} KB`
}
function formatModifiedAt(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const today = new Date()
  const elapsedDays = Math.floor((today.setHours(0, 0, 0, 0) - new Date(date).setHours(0, 0, 0, 0)) / 86400000)
  if (elapsedDays === 0) return '今天'
  if (elapsedDays >= 0 && elapsedDays < 7) return new Intl.DateTimeFormat('zh-CN', { weekday: 'long' }).format(date)
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(date)
}
function libraryFileIconSrc(item: LibraryItem) {
  return resolveColoredFileIcon(item.name, item.mimeType).src ?? ''
}
async function loadItems(options: { append?: boolean } = {}) {
  const append = options.append === true
  const nextPage = append ? currentPage.value + 1 : 1
  if (append) isLoadingMore.value = true
  else isLoading.value = true
  try {
    const result = await fetchLibraryItems(category.value, query.value, {
      scope: scope.value,
      source: source.value,
      fileType: fileType.value,
      page: nextPage,
      pageSize: libraryPageSize
    })
    items.value = append ? [...items.value, ...result.items] : result.items
    total.value = result.total
    currentPage.value = result.page
    hasMore.value = result.hasMore
  } catch {
    const fallback = scope.value === 'active' ? getMockLibraryItems(category.value, query.value) : { items: [], total: 0 }
    const nextItems = fallback.items.slice((nextPage - 1) * libraryPageSize, nextPage * libraryPageSize)
    items.value = append ? [...items.value, ...nextItems] : nextItems
    total.value = fallback.total
    currentPage.value = nextPage
    hasMore.value = currentPage.value * libraryPageSize < total.value
  } finally {
    selectedIds.value = selectedIds.value.filter((id) => items.value.some((item) => item.id === id))
    isLoading.value = false
    isLoadingMore.value = false
  }
}
function loadMore() { void loadItems({ append: true }) }
function scheduleLoad() { if (searchTimer) clearTimeout(searchTimer); searchTimer = setTimeout(() => void loadItems(), 180) }
function selectCategory(value: LibraryCategory) { category.value = value; void loadItems() }
function setSource(value: LibrarySourceFilter) { source.value = source.value === value ? 'all' : value; scope.value = 'active'; filterOpen.value = false; void loadItems() }
function setFileType(value: LibraryFileType) { fileType.value = fileType.value === value ? 'all' : value; scope.value = 'active'; filterOpen.value = false; void loadItems() }
function resetFileFilters() { source.value = 'all'; fileType.value = 'all'; scope.value = 'active'; filterOpen.value = false; void loadItems() }
function showDeleted() { scope.value = 'deleted'; filterOpen.value = false; void loadItems() }
function backToLibrary() { scope.value = 'active'; void loadItems() }
function selectItem(item: LibraryItem) { router.push({ name: 'document-preview', params: { itemId: item.id }, query: { from: 'library' } }) }
function openLibraryUpload() { libraryUploadInput.value?.click() }
async function handleLibraryUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  if (!files.length) return
  isUploading.value = true
  try {
    await uploadLibraryFiles(files)
    message.success(files.length === 1 ? '文件已上传至资料库' : `${files.length} 个文件已上传至资料库`)
    await loadItems()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '文件上传失败，请稍后重试')
  } finally {
    input.value = ''
    isUploading.value = false
  }
}
function iconOption(label: string, key: string, icon: Component, danger = false): DropdownOption {
  return {
    key,
    props: { class: danger ? 'library-action-danger' : '' },
    label: () => h('span', { class: ['library-row-action-option', danger ? 'library-action-danger' : ''] }, [
      h('span', { class: 'library-row-action-option__icon' }, [h(NIcon, { component: icon, size: 18 })]),
      h('span', { class: 'library-row-action-option__label' }, label)
    ])
  }
}
function rowActions(currentScope: LibraryScope): DropdownOption[] {
  return currentScope === 'deleted'
    ? [iconOption('恢复', 'restore', RefreshOutline), iconOption('永久删除', 'permanent-delete', TrashOutline, true)]
    : [iconOption('围绕此内容展开对话', 'start-chat', ChatbubblesOutline), iconOption('下载', 'download', DownloadOutline), iconOption('重命名', 'rename', CreateOutline), iconOption('删除', 'delete', TrashOutline, true)]
}
async function handleItemAction(action: string, item: LibraryItem) {
  try {
    if (action === 'start-chat') { await startChatWithItems([item]); return }
    if (action === 'download') { await downloadLibraryItem(item); return }
    if (action === 'rename') {
      renameTarget.value = item
      renameValue.value = item.name
      renameVisible.value = true
      void nextTick(() => {
        renameInput.value?.focus()
        renameInput.value?.select()
      })
      return
    }
    if (action === 'delete') { requestDelete([item]); return }
    if (action === 'restore') { await restoreLibraryItem(item.id); message.success('文件已恢复'); await loadItems(); return }
    if (action === 'permanent-delete') { await permanentlyDeleteLibraryItem(item.id); message.success('文件已永久删除'); await loadItems() }
  } catch (error) { message.error(error instanceof Error ? error.message : '操作失败，请稍后重试') }
}
function isSelected(itemId: string) { return selectedIds.value.includes(itemId) }
function toggleSelection(itemId: string) { selectedIds.value = isSelected(itemId) ? selectedIds.value.filter((id) => id !== itemId) : [...selectedIds.value, itemId] }
function toggleSelectAll() { selectedIds.value = allVisibleSelected.value ? [] : items.value.map((item) => item.id) }
async function downloadSelected() { try { for (const item of selectedItems.value) await downloadLibraryItem(item) } catch (error) { message.error(error instanceof Error ? error.message : '下载失败，请稍后重试') } }
function fileTypeForLibraryItem(item: LibraryItem): FileType { return item.extension.toLowerCase() === '.docx' ? 'supplemental_doc' : 'unknown' }
async function startChatWithItems(targets: LibraryItem[]) {
  if (!targets.length) return
  isStartingChat.value = true
  try {
    const conversation = await conversationStore.startNewConversation()
    for (const item of targets) {
      const { blob, filename } = await fetchLibraryItemBlob(item)
      const sourceFile = new File([blob], filename || item.name, { type: item.mimeType || blob.type || 'application/octet-stream' })
      const uploaded = await fileStore.uploadFile(sourceFile, conversation.id, fileTypeForLibraryItem(item))
      conversationStore.addDraftFile(conversation.id, uploaded)
    }
    selectedIds.value = []
    await router.push(`/chat/${encodeURIComponent(conversation.id)}`)
  } catch (error) { message.error(error instanceof Error ? error.message : '创建会话失败，请稍后重试') }
  finally { isStartingChat.value = false }
}
function requestDelete(targets: LibraryItem[]) { if (!targets.length) return; deleteCandidates.value = [...targets]; deleteConfirmVisible.value = true }
function closeDeleteConfirm() {
  if (isDeleting.value) return
  deleteConfirmVisible.value = false
  deleteCandidates.value = []
}
function closeRename() {
  if (isRenaming.value) return
  renameVisible.value = false
  renameTarget.value = null
}
async function confirmDelete() {
  if (isDeleting.value || !deleteCandidates.value.length) return
  isDeleting.value = true
  try { await Promise.all(deleteCandidates.value.map((item) => deleteLibraryItem(item.id))); selectedIds.value = []; deleteCandidates.value = []; deleteConfirmVisible.value = false; message.success('文件已移至最近删除'); await loadItems() }
  catch (error) { message.error(error instanceof Error ? error.message : '删除失败，请稍后重试') }
  finally { isDeleting.value = false }
}
async function submitRename() {
  if (isRenaming.value || !renameTarget.value || !renameValue.value.trim()) return
  isRenaming.value = true
  try { await renameLibraryItem(renameTarget.value.id, renameValue.value.trim()); message.success('文件已重命名'); renameVisible.value = false; renameTarget.value = null; await loadItems() }
  catch (error) { message.error(error instanceof Error ? error.message : '重命名失败') }
  finally { isRenaming.value = false }
}
onMounted(() => void loadItems())
onBeforeUnmount(() => { if (searchTimer) clearTimeout(searchTimer) })
</script>

<style scoped>
.library-page { flex: 1; min-width: 0; height: 100%; overflow-y: auto; background: #fff; }
.library-content { width: min(738px, calc(100% - 64px)); min-height: 100%; margin: 0 auto; padding: 116px 0 72px; }
.library-header { display: flex; gap: 24px; align-items: center; justify-content: space-between; }
.library-header h1 { margin: 0; color: #0d0d0d; font-size: 29px; font-weight: 600; letter-spacing: -.5px; line-height: 40px; }
.library-header__actions { display: flex; flex: 0 0 auto; gap: 14px; align-items: center; }
.library-search { width: 240px; }
.library-search :deep(.n-input) { height: 36px; border-radius: 18px; box-shadow: none; transition: border-color .12s ease, box-shadow .12s ease; }
.library-search :deep(.n-input.n-input--focus) { border-color: #777; box-shadow: 0 0 0 1px #777; }
.library-search :deep(.n-input__prefix) { color: #8e8e8e; }
.library-new { display: inline-flex; min-width: 88px; gap: 5px; align-items: center; justify-content: center; height: 36px; padding: 0 12px; color: #fff; font: inherit; font-size: 14px; font-weight: 600; line-height: 1; cursor: pointer; appearance: none; -webkit-appearance: none; background: #171717; border: 1px solid #171717; border-radius: 999px; box-shadow: none; }.library-new:disabled { cursor: wait; opacity: .72; }
.library-new:hover { background: #303030; border-color: #303030; }.library-new:focus-visible { outline: 2px solid #6b7280; outline-offset: 2px; }
.library-upload-input { display: none; }
.library-toolbar { display: flex; align-items: center; justify-content: space-between; min-height: 37px; margin-top: 51px; }.library-toolbar--selection { align-items: center; }
.library-tabs { display: flex; gap: 8px; align-items: center; }
.library-tab { height: 37px; padding: 0 16px; color: #4b5563; font: inherit; font-size: 14px; cursor: pointer; background: transparent; border: 0; border-radius: 20px; }
.library-tab:hover { background: #f5f5f5; }.library-tab--active { color: #111; background: #f2f2f2; font-weight: 500; }
.library-bulk-actions { display: flex; gap: 8px; align-items: center; }.library-bulk-actions :deep(.n-button) { height: 37px; padding: 0 14px; font-size: 14px; }.library-bulk-actions__chat { display: inline-flex; gap: 6px; align-items: center; justify-content: center; height: 37px; padding: 0 14px; color: #fff; font: inherit; font-size: 14px; line-height: 1; cursor: pointer; appearance: none; -webkit-appearance: none; background: #151515; border: 1px solid #151515; border-radius: 999px; box-shadow: none; }.library-bulk-actions__chat:hover { color: #fff; background: #303030; border-color: #303030; }.library-bulk-actions__chat:disabled { cursor: wait; opacity: .72; }.library-bulk-actions__chat:focus-visible { outline: 2px solid #6b7280; outline-offset: 2px; }.library-bulk-actions__delete { color: #e3202b; border-color: #ff4d5a; }.library-bulk-actions__delete:hover { color: #d91824; border-color: #e3202b; background: #fff7f7; }
.library-view-actions { display: flex; gap: 8px; align-items: center; }.library-icon-button { position: relative; display: grid; width: 36px; height: 36px; padding: 0; color: #8a8a8a; cursor: pointer; background: transparent; border: 0; border-radius: 50%; place-items: center; }.library-icon-button:hover, .library-icon-button--active { color: #202020; background: #f2f2f2; }.library-filter-badge { position: absolute; top: 0; right: -1px; min-width: 16px; height: 16px; padding: 0 4px; color: white; font-size: 10px; line-height: 16px; background: #111; border-radius: 10px; }.library-toolbar__divider { width: 1px; height: 22px; margin: 0 3px; background: #ededed; }
.library-selection-count { margin-right: 11px; color: #111; font-size: 14px; white-space: nowrap; }
.library-filter-menu { width: 240px; padding: 13px 10px 10px; }.library-filter-menu__label { margin: 0 9px 7px; color: #8a8a8a; font-size: 13px; }.library-filter-option { display: flex; width: 100%; gap: 11px; align-items: center; height: 36px; padding: 0 10px; color: #202020; font: inherit; font-size: 14px; text-align: left; cursor: pointer; background: transparent; border: 0; border-radius: 10px; }.library-filter-option:hover { background: #f2f2f2; }.library-filter-option :deep(.n-icon) { font-size: 18px; }.library-filter-divider { height: 1px; margin: 8px 0; background: #e9e9e9; }
.library-recycle-notice { display: flex; justify-content: space-between; align-items: center; min-height: 44px; margin-top: 18px; padding: 0 15px; color: #1f2937; font-size: 14px; background: #f1f1f1; border-radius: 14px; }.library-recycle-notice button { padding: 0; color: #737373; font: inherit; cursor: pointer; background: transparent; border: 0; }.library-recycle-notice button:hover { color: #111; }
.library-list { margin-top: 26px; }.library-list__head, .library-row { display: grid; grid-template-columns: minmax(0, 1fr) 191px 160px; column-gap: 0; }.library-list__head { position: relative; padding: 0 0 13px; color: #6b7280; font-size: 14px; }.library-list__select-all { position: absolute; left: -30px; display: grid; width: 19px; height: 19px; padding: 0; color: #fff; pointer-events: none; background: #111; border: 0; border-radius: 5px; opacity: 0; place-items: center; }.library-list__head--selecting .library-list__select-all { pointer-events: auto; opacity: 1; }
.library-row { position: relative; width: 100%; min-height: 60px; padding: 0; color: #374151; font: inherit; text-align: left; cursor: pointer; background: #fff; border: 0; border-bottom: 1px solid #f0f0f0; }.library-row:hover, .library-row--selected { z-index: 1; margin: 0 -10px; width: calc(100% + 20px); padding: 0 10px; background: #f3f3f3; border-bottom-color: transparent; border-radius: 12px; box-shadow: 0 7px 22px rgba(15, 23, 42, .045); }.library-row__selection { position: absolute; top: 20px; left: -31px; display: grid; width: 19px; height: 19px; padding: 0; color: #fff; pointer-events: none; cursor: pointer; background: #fff; border: 1px solid #d9d9d9; border-radius: 5px; opacity: 0; place-items: center; }.library-row:hover .library-row__selection, .library-row--selected .library-row__selection, .library-list:has(.library-row--selected) .library-row__selection { pointer-events: auto; opacity: 1; }.library-row__selection--selected { background: #111; border-color: #111; }.library-row__name { display: flex; min-width: 0; gap: 11px; align-items: center; color: #111; }.library-file-icon { display: grid; flex: 0 0 auto; width: 31px; height: 31px; overflow: hidden; color: #1683ff; background: #fff; border: 1px solid #e8e8e8; border-radius: 8px; place-items: center; }.library-file-icon--image { color: #6b7280; }.library-file-icon img { width: 100%; height: 100%; object-fit: cover; }.library-file-icon img.library-file-icon__asset { width: 20px; height: 20px; object-fit: contain; }.library-file-name { min-width: 0; overflow: hidden; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }.library-source { flex: 0 0 auto; padding: 2px 6px; color: #6b7280; font-size: 11px; background: #f4f4f4; border-radius: 4px; }.library-row__date, .library-row__size { display: flex; align-items: center; color: #4b5563; font-size: 14px; }.library-row__menu { position: absolute; top: 12px; right: 8px; visibility: hidden; opacity: 0; pointer-events: none; transition: opacity .12s ease; }.library-row:hover .library-row__menu, .library-row:focus-within .library-row__menu { visibility: visible; opacity: 1; pointer-events: auto; }.library-row__more { display: grid; width: 36px; height: 36px; padding: 0; color: #8a8a8a; cursor: pointer; background: #f1f1f1; border: 0; border-radius: 9px; place-items: center; }.library-row__more:hover { color: #222; background: #e8e8e8; }.library-row__menu :deep(.library-action-danger), .library-row__menu :deep(.library-action-danger .n-dropdown-option-body__label), :deep(.n-dropdown-option.library-action-danger), :deep(.n-dropdown-option.library-action-danger .n-dropdown-option-body__label) { color: #e4202b !important; }
.library-loading { display: grid; gap: 20px; margin-top: 32px; padding: 22px 0; }.library-empty { display: flex; flex-direction: column; gap: 13px; align-items: center; justify-content: center; min-height: 240px; margin-top: 32px; color: #1f2937; font-size: 14px; }.library-empty--deleted { min-height: 318px; margin-top: 26px; border: 1px dashed #d8d8d8; border-radius: 28px; }
.library-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-top: 26px; }.library-grid-card { min-width: 0; padding: 0; font: inherit; text-align: left; cursor: pointer; background: transparent; border: 0; }.library-grid-card__preview { display: grid; width: 100%; aspect-ratio: 1.42; overflow: hidden; color: #1683ff; background: #f7f7f7; border: 1px solid #ebebeb; border-radius: 10px; place-items: center; }.library-grid-card__preview--image { color: #6b7280; background: #f2f2f2; }.library-grid-card__preview img { width: 100%; height: 100%; object-fit: cover; }.library-grid-card__preview img.library-grid-file-icon__asset { width: 48px; height: 48px; object-fit: contain; }.library-grid-card__name { display: block; margin-top: 9px; overflow: hidden; color: #1f2937; font-size: 14px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }.library-grid-card__meta { display: block; margin-top: 3px; color: #6b7280; font-size: 12px; }
.library-dialog-backdrop { position: fixed; inset: 0; z-index: 10000; display: grid; padding: 20px; pointer-events: auto; background: rgba(0, 0, 0, .34); backdrop-filter: blur(2px); place-items: center; }
.library-rename-dialog { width: min(450px, calc(100vw - 40px)); padding: 18px 16px 16px; color: #171717; pointer-events: auto; background: #fff; border-radius: 16px; box-shadow: 0 18px 55px rgba(15,23,42,.2); }.library-rename-dialog h2 { margin: 0 0 16px; font-size: 18px; font-weight: 500; }.library-rename-dialog__actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 14px; }
.library-delete-modal { width: min(450px, calc(100vw - 40px)); padding: 22px 18px 16px; color: #171717; pointer-events: auto; background: #fff; border-radius: 16px; box-shadow: 0 18px 55px rgba(15,23,42,.2); }.library-delete-modal h2 { margin: 0; font-size: 18px; font-weight: 600; }.library-delete-modal p { margin: 16px 0 17px; font-size: 14px; line-height: 1.55; }.library-delete-modal__actions { display: flex; gap: 9px; justify-content: flex-end; }
@media (max-width: 860px) { .library-content { width: min(100% - 32px, 738px); padding-top: 36px; }.library-header { align-items: flex-start; flex-direction: column; gap: 18px; }.library-header__actions { width: 100%; }.library-search { flex: 1; width: auto; }.library-toolbar { margin-top: 32px; }.library-list__head { display: none; }.library-row { grid-template-columns: minmax(0, 1fr) 84px; }.library-row__size { display: none; }.library-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.library-recycle-notice { gap: 12px; align-items: flex-start; padding: 11px 15px; } }
:global(.n-dropdown-option.library-action-danger), :global(.n-dropdown-option.library-action-danger .n-dropdown-option-body), :global(.n-dropdown-option.library-action-danger .n-dropdown-option-body__label), :global(.n-dropdown-option.library-action-danger .n-dropdown-option-body__prefix) { color: #e4202b !important; }
:global(.library-row-action-menu) { overflow: hidden; border-radius: 18px !important; }
:global(.n-dropdown-option-body.library-action-danger), :global(.n-dropdown-option-body.library-action-danger .n-dropdown-option-body__label), :global(.n-dropdown-option-body.library-action-danger .n-dropdown-option-body__prefix) { color: #e4202b !important; }
.library-row { box-sizing: border-box; }.library-row:hover, .library-row--selected { padding: 0 10px; }.library-row__selection { left: -31px; z-index: 2; }
.library-list__select-all { left: -41px; }
.library-row-item { position: relative; box-sizing: border-box; width: calc(100% + 41px); min-height: 60px; margin-left: -41px; padding-left: 41px; }.library-row-item .library-row__selection { left: 0; }.library-row-item:hover .library-row, .library-row-item--selected .library-row { z-index: 1; margin: 0 -10px; width: calc(100% + 20px); padding: 0 10px; background: #f3f3f3; border-bottom-color: transparent; border-radius: 12px; box-shadow: 0 7px 22px rgba(15, 23, 42, .045); }.library-row-item:hover .library-row__selection, .library-row-item:focus-within .library-row__selection, .library-row-item--selected .library-row__selection, .library-list:has(.library-row-item--selected) .library-row__selection { pointer-events: auto; opacity: 1; }
.library-rename-dialog__input { box-sizing: border-box; display: block; width: 100%; height: 40px; padding: 0 12px; color: #171717; font: inherit; font-size: 14px; background: #fff; border: 1px solid #595959; border-radius: 7px; outline: none; }.library-rename-dialog__input:focus { border-color: #111; box-shadow: 0 0 0 1px #111; }.library-rename-dialog__cancel, .library-rename-dialog__submit, .library-delete-modal__cancel, .library-delete-modal__submit { min-width: 56px; height: 36px; padding: 0 14px; font: inherit; font-size: 14px; cursor: pointer; border-radius: 999px; }.library-rename-dialog__cancel, .library-delete-modal__cancel { color: #171717; background: #fff; border: 1px solid #dedede; }.library-rename-dialog__submit { color: #fff; background: #171717; border: 1px solid #171717; }.library-rename-dialog__submit:hover:not(:disabled) { background: #303030; border-color: #303030; }.library-delete-modal__submit { color: #fff; background: #e94f67; border: 1px solid #e94f67; }.library-delete-modal__submit:hover:not(:disabled) { background: #dc3f59; border-color: #dc3f59; }.library-rename-dialog__cancel:disabled, .library-rename-dialog__submit:disabled, .library-delete-modal__cancel:disabled, .library-delete-modal__submit:disabled { cursor: wait; opacity: .65; }
.library-load-more-wrap { display: flex; justify-content: center; padding: 28px 0 8px; }.library-load-more { min-width: 170px; height: 36px; padding: 0 16px; color: #303030; font: inherit; font-size: 13px; cursor: pointer; background: #fff; border: 1px solid #dedede; border-radius: 999px; }.library-load-more:hover:not(:disabled) { background: #f5f5f5; }.library-load-more:disabled { cursor: wait; opacity: .62; }
.library-file-icon img.library-file-icon__asset { width: 17px; height: 17px; }
.library-grid-card__preview img.library-grid-file-icon__asset { width: 41px; height: 41px; }
.library-filter-option { justify-content: space-between; }
.library-filter-option__content { display: inline-flex; min-width: 0; gap: 11px; align-items: center; }
.library-filter-option__check { flex: 0 0 auto; width: 15px; height: 15px; object-fit: contain; }
</style>

<style>
.library-row-action-menu .n-dropdown-option-body__prefix { display: none; width: 0; }
.library-row-action-menu .n-dropdown-option-body__label { width: 100%; }
.library-row-action-option { display: flex; width: 100%; gap: 12px; align-items: center; }
.library-row-action-option__icon { display: grid; flex: 0 0 18px; width: 18px; height: 18px; place-items: center; }
.library-row-action-option__icon .n-icon { display: flex; }
.library-row-action-option__label { line-height: 20px; }
</style>
