import { describe, expect, it } from 'vitest'
import source from './LibraryView.vue?raw'

describe('LibraryView row action affordance', () => {
  it('anchors the ellipsis trigger in a dedicated row wrapper', () => {
    expect(source).toContain('<span class="library-row__menu">')
    expect(source).not.toContain('<n-dropdown class="library-row__menu"')
  })

  it('uses stable dropdown hooks for the rounded menu and its danger action', () => {
    expect(source).toContain(':menu-props="libraryRowMenuProps"')
    expect(source).toContain('.library-row-action-menu')
    expect(source).toContain('.n-dropdown-option-body.library-action-danger')
  })

  it('uses one fixed icon column so every row action remains aligned', () => {
    expect(source).toContain("class: 'library-row-action-option__icon'")
    expect(source).toContain('.library-row-action-menu .n-dropdown-option-body__prefix { display: none;')
    expect(source).toContain('.library-row-action-option__icon { display: grid; flex: 0 0 18px;')
    expect(source).not.toContain("icon: () => h(NIcon")
  })

  it('keeps the checkbox in an independent left selection column and uses a custom rename dialog', () => {
    expect(source).toContain('class="library-rename-dialog"')
    expect(source).toContain('class="library-row-item"')
    expect(source).toContain('.library-row-item:hover .library-row__selection')
    expect(source).toContain('.library-row-item { position: relative; box-sizing: border-box; width: calc(100% + 41px); min-height: 60px; margin-left: -41px; padding-left: 41px; }')
    expect(source).toContain('.library-row-item .library-row__selection { left: 0;')
    expect(source).toContain('.library-row:hover, .library-row--selected { padding: 0 10px;')
    expect(source).not.toContain('preset="card" class="library-rename-modal"')
  })

  it('aligns select-all with the independent checkbox column without shifting file content', () => {
    expect(source).toContain('.library-list__select-all { left: -41px;')
    expect(source).not.toContain('.library-list:has(.library-row--selected) .library-row { padding: 0 10px 0 44px; }')
  })

  it('uses native black pill buttons without nested UI border layers', () => {
    expect(source).toContain('<button class="library-new" type="button" :disabled="isUploading" @click="openLibraryUpload">')
    expect(source).toContain('<button class="library-bulk-actions__chat" type="button"')
    expect(source).toContain('appearance: none;')
  })

  it('opens a direct file picker from the upload button instead of a dropdown', () => {
    expect(source).toContain('<input ref="libraryUploadInput" class="library-upload-input" type="file" multiple')
    expect(source).toContain('@change="handleLibraryUpload"')
    expect(source).toContain('<button class="library-new" type="button" :disabled="isUploading" @click="openLibraryUpload">')
    expect(source).toContain('uploadLibraryFiles')
    expect(source).not.toContain('newItemOptions')
  })

  it('uses color file-svg assets inside the existing Library icon squares', () => {
    expect(source).toContain("import { resolveColoredFileIcon } from '@/utils/fileIcon'")
    expect(source).toContain('class="library-file-icon__asset"')
    expect(source).toContain('class="library-grid-file-icon__asset"')
    expect(source).toContain('function libraryFileIconSrc(item: LibraryItem)')
    expect(source).toContain('library-file-icon img.library-file-icon__asset')
  })

  it('shows the supplied checkmark and an accurate filter-count badge for active Library filters', () => {
    expect(source).toContain("import checkmarkIcon from '@/assets/checkmark.svg'")
    expect(source).toContain('const activeFilterCount = computed(')
    expect(source).toContain('<span v-if="activeFilterCount" class="library-filter-badge">{{ activeFilterCount }}</span>')
    expect(source).toContain('class="library-filter-option__check"')
    expect(source).toContain(':src="checkmarkIcon"')
    expect(source).toContain('.library-filter-option:hover { background: #f2f2f2; }')
    expect(source).not.toContain('.library-filter-option:hover, .library-filter-option.active { background: #f2f2f2; }')
  })

  it('keeps rename editable and its primary action enabled by default', () => {
    expect(source).toContain('class="library-rename-dialog__input"')
    expect(source).toContain('class="library-rename-dialog__submit" type="button"')
    expect(source).not.toContain(':disabled="!renameCanSubmit"')
  })

  it('reuses an existing empty conversation for chat-from-file', () => {
    expect(source).toContain('const conversation = await conversationStore.startNewConversation()')
    expect(source).not.toContain('const conversation = await conversationStore.createNewConversation()')
  })

  it('invokes rename and delete actions from an interaction-safe teleported layer', () => {
    expect(source).toContain('<Teleport to="body">')
    expect(source).toContain('class="library-dialog-backdrop"')
    expect(source).toContain('@keydown.enter.prevent="submitRename"')
    expect(source).toContain('@click="submitRename"')
    expect(source).toContain('@click="confirmDelete"')
    expect(source).toContain('renameInput.value?.focus()')
    expect(source).not.toContain('<n-modal')
    expect(source).not.toContain('@click="void submitRename"')
    expect(source).not.toContain('@click="void confirmDelete"')
    expect(source).not.toMatch(/@[a-zA-Z:-]+="void [a-zA-Z0-9_]+"/)
  })
})
