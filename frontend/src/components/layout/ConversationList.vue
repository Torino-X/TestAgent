<template>
  <div ref="listElement" class="conversation-list" @scroll.passive="queueOverflowReport">
    <div class="conversation-list__title">历史记录</div>

    <section class="conversation-list__section" aria-label="已置顶">
      <button
        class="conversation-list__section-heading"
        type="button"
        :aria-expanded="isPinnedExpanded"
        @click="isPinnedExpanded = !isPinnedExpanded"
      >
        <span class="conversation-list__section-title">已置顶</span>
        <n-icon
          class="conversation-list__chevron"
          :class="{ 'is-collapsed': !isPinnedExpanded }"
          :component="ChevronDownOutline"
        />
      </button>
      <ul v-show="isPinnedExpanded" class="conversation-list__items conversation-list__items--pinned">
        <li v-for="conversation in pinnedConversations" :key="conversation.id" class="conversation-row">
          <RouterLink
            class="conversation-item"
            :class="{ active: activeId === conversation.id }"
            :to="`/chat/${conversation.id}`"
          >
            <input
              v-if="editingConversationId === conversation.id"
              v-model="renameDraft"
              class="conversation-item__rename-input"
              type="text"
              @click.prevent.stop
              @keydown.enter.prevent="submitRename(conversation)"
              @keydown.esc.prevent="cancelRename"
              @blur="submitRename(conversation)"
            />
            <span v-else class="conversation-item__label">
              <span class="conversation-item__title" :title="conversation.title">{{ conversation.title }}</span>
              <span v-if="conversation.projectName" class="conversation-item__project" :title="conversation.projectName">{{ conversation.projectName }}</span>
            </span>
            <span class="conversation-item__actions">
              <button
                class="conversation-item__icon-button conversation-item__pin is-pinned"
                type="button"
                title="取消置顶"
                @click.prevent.stop="togglePin(conversation.id)"
              >
                <img class="pin-icon" :src="pinIconUrl" alt="" aria-hidden="true" />
              </button>
              <button
                class="conversation-item__icon-button conversation-item__more"
                type="button"
                title="更多操作"
                @click.prevent.stop="toggleMenu(conversation.id, $event)"
              >
                <n-icon :component="EllipsisHorizontal" />
              </button>
            </span>
          </RouterLink>
        </li>
      </ul>
    </section>

    <section class="conversation-list__section conversation-list__section--recent" aria-label="最近">
      <button
        class="conversation-list__section-heading"
        type="button"
        :aria-expanded="isRecentExpanded"
        @click="isRecentExpanded = !isRecentExpanded"
      >
        <span class="conversation-list__section-title">最近</span>
        <n-icon
          class="conversation-list__chevron"
          :class="{ 'is-collapsed': !isRecentExpanded }"
          :component="ChevronDownOutline"
        />
      </button>

      <ul v-show="isRecentExpanded" class="conversation-list__items conversation-list__items--recent">
        <li v-for="conversation in recentConversations" :key="conversation.id" class="conversation-row">
          <RouterLink
            class="conversation-item"
            :class="{ active: activeId === conversation.id }"
            :to="`/chat/${conversation.id}`"
          >
            <input
              v-if="editingConversationId === conversation.id"
              v-model="renameDraft"
              class="conversation-item__rename-input"
              type="text"
              @click.prevent.stop
              @keydown.enter.prevent="submitRename(conversation)"
              @keydown.esc.prevent="cancelRename"
              @blur="submitRename(conversation)"
            />
            <span v-else class="conversation-item__label">
              <span class="conversation-item__title" :title="conversation.title">{{ conversation.title }}</span>
              <span v-if="conversation.projectName" class="conversation-item__project" :title="conversation.projectName">{{ conversation.projectName }}</span>
            </span>
            <span class="conversation-item__actions">
              <button
                class="conversation-item__icon-button conversation-item__pin"
                type="button"
                title="置顶聊天"
                @click.prevent.stop="togglePin(conversation.id)"
              >
                <img class="pin-icon" :src="pinIconUrl" alt="" aria-hidden="true" />
              </button>
              <button
                class="conversation-item__icon-button conversation-item__more"
                type="button"
                title="更多操作"
                @click.prevent.stop="toggleMenu(conversation.id, $event)"
              >
                <n-icon :component="EllipsisHorizontal" />
              </button>
            </span>
          </RouterLink>
        </li>
      </ul>
    </section>
  </div>

  <Teleport to="body">
    <div
      v-if="menuConversation"
      class="history-menu history-menu--teleported"
      :style="menuStyle"
      @click.stop
    >
      <button class="history-menu__item" type="button" @click.prevent.stop="startRename(menuConversation)">
        <n-icon :component="CreateOutline" />
        <span>重命名</span>
      </button>
      <button class="history-menu__item" type="button" @click.prevent.stop="togglePin(menuConversation.id)">
        <img class="history-menu__pin-icon" :src="pinIconUrl" alt="" aria-hidden="true" />
        <span>{{ pinnedIds.has(menuConversation.id) ? '取消置顶' : '置顶聊天' }}</span>
      </button>
      <button class="history-menu__item" type="button" @click.prevent.stop="moveToProject(menuConversation)">
        <n-icon :component="FolderOutline" />
        <span>移动到项目</span>
      </button>
      <div class="history-menu__divider" />
      <button
        class="history-menu__item history-menu__item--danger"
        type="button"
        @click.prevent.stop="confirmDelete(menuConversation)"
      >
        <n-icon :component="TrashOutline" />
        <span>删除记录</span>
      </button>
    </div>
  </Teleport>

  <ConfirmDangerDialog
    :visible="pendingDeleteConversation !== null"
    title="删除聊天？"
    hint="删除后，该记录将从历史记录中移除。"
    @cancel="cancelDelete"
    @confirm="confirmDeleteNow"
  >
    <template #body>
      这会删除“<strong>{{ pendingDeleteConversation?.title }}</strong>”。
    </template>
  </ConfirmDangerDialog>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import {
  ChevronDownOutline,
  CreateOutline,
  EllipsisHorizontal,
  FolderOutline,
  TrashOutline
} from '@vicons/ionicons5'
import type { Conversation } from '@/types'
import pinIconUrl from '@/assets/icons/pin.svg'
import ConfirmDangerDialog from '@/components/common/ConfirmDangerDialog.vue'

const props = defineProps<{
  conversations: Conversation[]
  activeId: string
}>()

const emit = defineEmits<{
  (event: 'delete', id: string): void
  (event: 'rename', payload: { id: string; title: string }): void
  (event: 'move-to-project', conversation: Conversation): void
  (event: 'overflow-change', isOverflowing: boolean): void
}>()

const listElement = ref<HTMLElement | null>(null)
const pinnedIds = ref(new Set<string>())
const openMenuId = ref<string | null>(null)
const menuStyle = ref<Record<string, string>>({})
const menuConversation = ref<Conversation | null>(null)
const editingConversationId = ref<string | null>(null)
const renameDraft = ref('')
const isPinnedExpanded = ref(true)
const isRecentExpanded = ref(true)
const pendingDeleteConversation = ref<Conversation | null>(null)
let resizeObserver: ResizeObserver | undefined
let mutationObserver: MutationObserver | undefined
let lastOverflowState: boolean | undefined
let overflowReportFrame: number | undefined

const historyConversations = computed(() => props.conversations.filter((item) => item.id !== 'conv_new'))
const pinnedConversations = computed(() =>
  historyConversations.value.filter((conversation) => pinnedIds.value.has(conversation.id))
)
const recentConversations = computed(() =>
  historyConversations.value.filter((conversation) => !pinnedIds.value.has(conversation.id))
)

function togglePin(id: string) {
  const next = new Set(pinnedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  pinnedIds.value = next
  closeMenu()
}

function toggleMenu(id: string, event: MouseEvent) {
  if (openMenuId.value === id) {
    closeMenu()
    return
  }
  const btn = (event.currentTarget as HTMLElement).closest('.conversation-item__more') as HTMLElement | null
  if (!btn) { closeMenu(); return }
  const rect = btn.getBoundingClientRect()
  const menuW = 150
  const menuH = 140
  const gap = 8
  const vw = window.innerWidth
  const vh = window.innerHeight
  let left = rect.right + gap
  if (left + menuW > vw) left = rect.left - menuW - gap
  let top = rect.top
  if (top + menuH > vh) top = vh - menuH - 4
  if (top < 0) top = 4
  menuStyle.value = { position: 'fixed', left: `${left}px`, top: `${top}px`, zIndex: '9999' }
  const conv = [...pinnedConversations.value, ...recentConversations.value].find(c => c.id === id) ?? null
  menuConversation.value = conv
  openMenuId.value = id
}

function handleDocumentClick() {
  closeMenu()
}

function closeMenu() {
  openMenuId.value = null
  menuConversation.value = null
  menuStyle.value = {}
}

function startRename(conversation: Conversation) {
  closeMenu()
  editingConversationId.value = conversation.id
  renameDraft.value = conversation.title
  void nextTick(() => {
    const input = document.querySelector('.conversation-item__rename-input') as HTMLInputElement | null
    if (input) {
      input.focus()
      input.select()
    }
  })
}

function cancelRename() {
  editingConversationId.value = null
  renameDraft.value = ''
}

function submitRename(conversation: Conversation) {
  const title = renameDraft.value.trim()
  if (!title || title === conversation.title) {
    cancelRename()
    return
  }
  emit('rename', { id: conversation.id, title })
  cancelRename()
}

function confirmDelete(conversation: Conversation) {
  closeMenu()
  pendingDeleteConversation.value = conversation
}

function moveToProject(conversation: Conversation) {
  closeMenu()
  emit('move-to-project', conversation)
}

function cancelDelete() {
  pendingDeleteConversation.value = null
}

function confirmDeleteNow() {
  const target = pendingDeleteConversation.value
  pendingDeleteConversation.value = null
  if (target) emit('delete', target.id)
}

function reportOverflow() {
  const element = listElement.value
  const shouldShowDivider = Boolean(
    element
    && element.scrollHeight > element.clientHeight + 1
    && element.scrollTop > 1
  )
  if (lastOverflowState === shouldShowDivider) return
  lastOverflowState = shouldShowDivider
  emit('overflow-change', shouldShowDivider)
}

function queueOverflowReport() {
  if (overflowReportFrame !== undefined) cancelAnimationFrame(overflowReportFrame)
  overflowReportFrame = requestAnimationFrame(() => {
    overflowReportFrame = undefined
    reportOverflow()
  })
}

onMounted(() => {
  document.addEventListener('click', handleDocumentClick)
  resizeObserver = new ResizeObserver(queueOverflowReport)
  mutationObserver = new MutationObserver(queueOverflowReport)
  if (listElement.value) {
    resizeObserver.observe(listElement.value)
    mutationObserver.observe(listElement.value, { childList: true, subtree: true, characterData: true })
  }
  window.addEventListener('resize', queueOverflowReport)
  void nextTick(queueOverflowReport)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', handleDocumentClick)
  window.removeEventListener('resize', queueOverflowReport)
  resizeObserver?.disconnect()
  mutationObserver?.disconnect()
  if (overflowReportFrame !== undefined) cancelAnimationFrame(overflowReportFrame)
})

watch(
  [() => props.conversations, isPinnedExpanded, isRecentExpanded],
  () => { void nextTick(queueOverflowReport) },
  { deep: true }
)
</script>

<style scoped>
.conversation-list {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  padding: 8px 10px 16px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: #d1d1d1 transparent;
}

.conversation-list::-webkit-scrollbar {
  width: 5px;
}

.conversation-list::-webkit-scrollbar-thumb {
  background: #d1d1d1;
}

.conversation-list__title {
  padding: 8px 8px 12px;
  color: var(--text-tertiary);
  font-size: var(--text-meta);
  font-weight: var(--font-regular);
  line-height: var(--leading-meta);
  background: var(--ta-surface);
}

.conversation-list__section {
  margin-bottom: 20px;
}

.conversation-list__section--recent {
  flex: 1 1 auto;
  min-height: 0;
}

.conversation-list__section-title {
  color: var(--text-tertiary);
  font-size: var(--text-meta);
  font-weight: var(--font-regular);
  line-height: var(--leading-meta);
}

.conversation-list__section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  margin-bottom: 2px;
  padding: 4px 8px;
  color: var(--ta-text-muted);
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: var(--ta-radius-sm);
  transition: background 160ms ease;
}

.conversation-list__section-heading:hover {
  background: var(--ta-surface-low);
}

.conversation-list__chevron {
  font-size: 14px;
  transition: transform 160ms ease;
}

.conversation-list__chevron.is-collapsed {
  transform: rotate(-90deg);
}

.conversation-list__items {
  padding: 0;
  margin: 0;
  list-style: none;
}

.conversation-row {
  position: relative;
  margin-bottom: 2px;
}

.conversation-item {
  display: flex;
  align-items: center;
  height: 36px;
  min-width: 0;
  padding: 0 8px;
  color: var(--ta-text-strong);
  border-radius: var(--ta-radius-sm);
  transition:
    color 160ms ease,
    background 160ms ease;
}

.conversation-item:hover,
.conversation-item.active {
  background: #ececec;
}

.conversation-item__label {
  display: flex;
  flex: 1 1 auto;
  gap: 7px;
  align-items: center;
  min-width: 0;
}

.conversation-item__title {
  flex: 1 1 auto;
  min-width: 24px;
  overflow: hidden;
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-meta);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-item__project {
  flex: 0 1 72px;
  min-width: 26px;
  overflow: hidden;
  color: #8f8f8f;
  font-size: 12px;
  font-weight: 400;
  line-height: var(--leading-meta);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-item__rename-input {
  flex: 1;
  min-width: 0;
  height: 26px;
  padding: 0 6px;
  color: var(--ta-text-strong);
  font: inherit;
  background: #ffffff;
  border: 1px solid #c6c6c6;
  border-radius: var(--ta-radius-sm);
  outline: none;
}

.conversation-item__rename-input:focus {
  border-color: var(--ta-primary);
  box-shadow: 0 0 0 2px rgba(0, 0, 0, 0.1);
}

.conversation-item__actions {
  display: flex;
  flex: 0 0 auto;
  gap: 2px;
  align-items: center;
  margin-left: 6px;
  opacity: 0;
  transition: opacity 160ms ease;
}

.conversation-item:hover .conversation-item__actions,
.conversation-item.active .conversation-item__actions {
  opacity: 1;
}

.conversation-item__icon-button {
  display: grid;
  width: 22px;
  height: 22px;
  padding: 0;
  color: var(--ta-text-muted);
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: var(--ta-radius-sm);
  place-items: center;
}

.conversation-item__icon-button:hover {
  color: var(--ta-text-strong);
  background: rgba(255, 255, 255, 0.62);
}

.conversation-item__pin.is-pinned {
  flex: 0 0 auto;
  color: var(--ta-text-muted);
  opacity: 1;
}

.pin-icon,
.history-menu__pin-icon {
  width: 14px;
  height: 14px;
  color: currentColor;
}

.history-menu__pin-icon {
  flex: 0 0 auto;
  margin-right: 0;
}

.history-menu {
  position: fixed;
  z-index: 9999;
  width: 150px;
  padding: 4px 0;
  background: #ffffff;
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-md);
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
}

.history-menu--teleported {
  pointer-events: auto;
}

.history-menu__item {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 7px 12px;
  color: var(--ta-text);
  font-size: 14px;
  font-weight: 400;
  line-height: 20px;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.history-menu__item:hover {
  background: var(--ta-surface-low);
}

.history-menu__item--danger {
  color: var(--ta-error);
}

.history-menu__item--danger:hover {
  background: #fff1f2;
}

.history-menu__divider {
  height: 1px;
  margin: 4px 8px;
  background: var(--ta-border);
}
</style>
