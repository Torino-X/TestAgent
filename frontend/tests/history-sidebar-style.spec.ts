import { describe, expect, it } from 'vitest'
import conversationListSource from '../src/components/layout/ConversationList.vue?raw'
import confirmDangerDialogSource from '../src/components/common/ConfirmDangerDialog.vue?raw'
import knowledgeDocumentManagerSource from '../src/components/knowledge/KnowledgeDocumentManager.vue?raw'
import knowledgeChunkManagerSource from '../src/components/knowledge/KnowledgeChunkManager.vue?raw'
import sidebarSource from '../src/components/layout/Sidebar.vue?raw'
import appSource from '../src/App.vue?raw'

describe('history sidebar style contract', () => {
  it('matches the Stitch grouped history layout', () => {
    expect(conversationListSource).toContain('conversation-list__section-title')
    expect(conversationListSource).toContain('已置顶')
    expect(conversationListSource).toContain('最近')
    expect(conversationListSource).toContain('conversation-item__pin')
    expect(conversationListSource).toContain('conversation-item__more')
    expect(conversationListSource).toContain('history-menu')
    expect(conversationListSource).toContain('重命名')
    expect(conversationListSource).toContain('置顶聊天')
    expect(conversationListSource).toContain('删除记录')
  })

  it('keeps delete as a history-list action without changing the rest of the sidebar', () => {
    expect(conversationListSource).toContain("emit('delete', target.id)")
    expect(sidebarSource).toContain('@delete="handleDeleteConversation"')
    expect(sidebarSource).toContain('conversationStore.deleteConversation(id)')
  })

  it('supports renaming a history record from the action menu', () => {
    expect(conversationListSource).toContain("'rename'")
    expect(conversationListSource).toContain('editingConversationId')
    expect(conversationListSource).toContain('renameDraft')
    expect(conversationListSource).toContain('startRename(menuConversation)')
    expect(conversationListSource).toContain('submitRename(conversation)')
    expect(conversationListSource).toContain('@keydown.enter.prevent')
    expect(conversationListSource).toContain('@keydown.esc.prevent')
    expect(sidebarSource).toContain('@rename="handleRenameConversation"')
    expect(sidebarSource).toContain('conversationStore.updateConversationTitle(id, title)')
  })

  it('shows the same action menu affordance for pinned history records', () => {
    expect(conversationListSource).toContain('conversation-list__items conversation-list__items--pinned')
    expect(conversationListSource).toContain('conversation-list__items conversation-list__items--recent')
    expect(conversationListSource.match(/conversation-item__more/g)?.length ?? 0).toBeGreaterThanOrEqual(2)
    expect(conversationListSource).toContain('class="history-menu history-menu--teleported"')
    expect(conversationListSource).toContain('toggleMenu(conversation.id, $event)')
  })

  it('uses a custom ChatGPT-style delete confirmation modal instead of browser confirm', () => {
    expect(conversationListSource).toContain('<ConfirmDangerDialog')
    expect(conversationListSource).toContain('pendingDeleteConversation')
    expect(conversationListSource).toContain('删除聊天？')
    expect(conversationListSource).toContain('删除后，该记录将从历史记录中移除。')
    expect(conversationListSource).toContain('confirmDeleteNow')
    expect(conversationListSource).not.toContain('window.confirm')

    expect(confirmDangerDialogSource).toContain('delete-confirm')
    expect(confirmDangerDialogSource).toContain('delete-confirm__dialog')
    expect(confirmDangerDialogSource).toContain('delete-confirm__button--danger')
    expect(confirmDangerDialogSource).toContain('background: #f20d32')
    expect(confirmDangerDialogSource).toContain('border-radius: 14px')
  })

  it('knowledge document delete uses the shared custom confirmation modal', () => {
    expect(knowledgeDocumentManagerSource).toContain('<ConfirmDangerDialog')
    expect(knowledgeDocumentManagerSource).toContain('pendingDeleteDoc')
    expect(knowledgeDocumentManagerSource).toContain('删除文档？')
    expect(knowledgeDocumentManagerSource).toContain('confirmDeleteDocNow')
    expect(knowledgeDocumentManagerSource).toContain('cancelDeleteDoc')
    expect(knowledgeDocumentManagerSource).not.toContain('window.confirm')
  })

  it('knowledge chunk delete uses the shared custom confirmation modal', () => {
    expect(knowledgeChunkManagerSource).toContain('<ConfirmDangerDialog')
    expect(knowledgeChunkManagerSource).toContain('pendingDeleteChunk')
    expect(knowledgeChunkManagerSource).toContain('删除分片？')
    expect(knowledgeChunkManagerSource).toContain('confirmDeleteChunkNow')
    expect(knowledgeChunkManagerSource).toContain('cancelDeleteChunk')
    expect(knowledgeChunkManagerSource).not.toContain('window.confirm')
  })

  it('uses Stitch list density and hover-only action affordances', () => {
    expect(conversationListSource).toContain('height: 36px')
    expect(conversationListSource).toContain('.conversation-item:hover .conversation-item__actions')
    expect(conversationListSource).toContain('.conversation-item.active .conversation-item__actions')
  })

  it('supports collapsing both pinned and recent groups', () => {
    expect(conversationListSource).toContain('isPinnedExpanded')
    expect(conversationListSource).toContain('isRecentExpanded')
    expect(conversationListSource).toContain('v-show="isPinnedExpanded"')
    expect(conversationListSource).toContain('v-show="isRecentExpanded"')
    expect(conversationListSource).toContain('@click="isPinnedExpanded = !isPinnedExpanded"')
    expect(conversationListSource).toContain('@click="isRecentExpanded = !isRecentExpanded"')
  })

  it('uses the provided pin svg asset instead of the default icon library pin', () => {
    expect(conversationListSource).toContain("import pinIconUrl from '@/assets/icons/pin.svg'")
    expect(conversationListSource).toContain(':src="pinIconUrl"')
    expect(conversationListSource).not.toContain('PinOutline')
  })

  it('keeps the sidebar footer pinned to the bottom independently of history length', () => {
    expect(sidebarSource).toContain('margin-top: auto')
  })

  it('lets the history list use the full space above the footer', () => {
    expect(conversationListSource).toContain('height: 100%')
    expect(conversationListSource).toContain('display: flex')
    expect(conversationListSource).toContain('flex-direction: column')
    expect(conversationListSource).toContain('conversation-list__section--recent')
    expect(conversationListSource).toContain('flex: 1 1 auto')
    expect(sidebarSource).toContain('overflow: hidden')
  })

  it('closes an open history action menu when clicking outside it', () => {
    expect(conversationListSource).toContain("from 'vue'")
    expect(conversationListSource).toContain('onMounted')
    expect(conversationListSource).toContain('onBeforeUnmount')
    expect(conversationListSource).toContain('document.addEventListener')
    expect(conversationListSource).toContain('document.removeEventListener')
    expect(conversationListSource).toContain('handleDocumentClick')
    expect(conversationListSource).toContain('class="history-menu history-menu--teleported"')
    expect(conversationListSource).toContain('@click.stop')
  })

  it('uses a ChatGPT-like system font treatment for the sidebar', () => {
    expect(appSource).toContain('BlinkMacSystemFont')
    expect(appSource).toContain('"Segoe UI"')
    expect(sidebarSource).toContain('font-size: var(--text-ui)')
    expect(sidebarSource).toContain('font-weight: var(--font-regular)')
    expect(conversationListSource).toContain('font-weight: var(--font-regular)')
    expect(conversationListSource).toContain('font-weight: 400')
  })

  it('shows an ellipsized light-gray Project name after a Project conversation title', () => {
    expect(conversationListSource.match(/conversation-item__project/g)?.length ?? 0).toBeGreaterThanOrEqual(2)
    expect(conversationListSource).toContain('v-if="conversation.projectName"')
    expect(conversationListSource).toContain('{{ conversation.projectName }}')
    expect(conversationListSource).toContain('color: #8f8f8f')
    expect(conversationListSource).toContain('text-overflow: ellipsis')
    expect(conversationListSource).toContain('white-space: nowrap')
  })
})
