<template>
  <Teleport to="body">
    <div v-if="show" class="library-picker-modal" @click.self="emit('close')" @keydown.esc="emit('close')">
      <section class="library-picker" role="dialog" aria-modal="true" aria-labelledby="library-picker-title">
        <header class="library-picker__header">
          <div>
            <h2 id="library-picker-title">资料库上传</h2>
            <p>选择后会作为附件加入当前对话，不会自动发送。</p>
          </div>
          <button type="button" aria-label="关闭" :disabled="Boolean(attachingId)" @click="emit('close')">×</button>
        </header>

        <label class="library-picker__search">
          <n-icon :component="SearchOutline" />
          <input v-model="query" type="search" placeholder="搜索资料库文档" />
        </label>

        <nav class="library-picker__categories" aria-label="资料库文件类型">
          <button
            v-for="entry in categories"
            :key="entry.value"
            type="button"
            :class="{ active: category === entry.value }"
            @click="category = entry.value"
          >
            {{ entry.label }}
          </button>
        </nav>

        <div v-if="loading" class="library-picker__state">正在加载资料库…</div>
        <div v-else-if="error" class="library-picker__state library-picker__state--error">
          <span>{{ error }}</span>
          <button type="button" @click="emit('retry')">重试</button>
        </div>
        <div v-else-if="filteredItems.length" class="library-picker__list" role="list">
          <button
            v-for="item in filteredItems"
            :key="item.id"
            class="library-picker__row"
            type="button"
            role="listitem"
            :disabled="Boolean(attachingId)"
            @click="emit('select', item)"
          >
            <span class="library-picker__file-icon">
              <img v-if="iconFor(item)" :src="iconFor(item)" alt="" />
              <n-icon v-else :component="DocumentTextOutline" />
            </span>
            <span class="library-picker__file-info">
              <strong>{{ item.name }}</strong>
              <small>{{ fileKindLabel(item) }} · {{ formatSize(item.sizeBytes) }}</small>
            </span>
            <span v-if="attachingId === item.id" class="library-picker__adding">正在添加</span>
            <n-icon v-else :component="ChevronForwardOutline" />
          </button>
        </div>
        <div v-else class="library-picker__empty">
          <n-icon :component="DocumentsOutline" :size="32" />
          <strong>{{ query ? '没有找到匹配的文档' : '资料库中暂无可用文档' }}</strong>
          <p>请先前往资料库上传文档。</p>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { ChevronForwardOutline, DocumentsOutline, DocumentTextOutline, SearchOutline } from '@vicons/ionicons5'
import type { LibraryItem } from '@/types/library'
import { resolveColoredFileIcon } from '@/utils/fileIcon'

type LibraryPickerCategory = 'all' | 'document' | 'spreadsheet' | 'presentation' | 'pdf'

const props = withDefaults(defineProps<{
  show: boolean
  items?: LibraryItem[]
  loading?: boolean
  error?: string
  attachingId?: string
}>(), {
  items: () => [],
  loading: false,
  error: '',
  attachingId: ''
})

const emit = defineEmits<{
  close: []
  retry: []
  select: [item: LibraryItem]
}>()

const query = ref('')
const category = ref<LibraryPickerCategory>('all')
const categories: Array<{ value: LibraryPickerCategory; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'document', label: '文档' },
  { value: 'spreadsheet', label: '电子表格' },
  { value: 'presentation', label: '演示文稿' },
  { value: 'pdf', label: 'PDF' }
]

const filteredItems = computed(() => {
  const normalizedQuery = query.value.trim().toLowerCase()
  return props.items.filter((item) => {
    const categoryMatches = category.value === 'all' || fileCategory(item) === category.value
    const queryMatches = !normalizedQuery || item.name.toLowerCase().includes(normalizedQuery)
    return categoryMatches && queryMatches
  })
})

watch(() => props.show, (visible) => {
  if (!visible) return
  query.value = ''
  category.value = 'all'
})

function normalizedExtension(item: LibraryItem) {
  return (item.extension || item.name.split('.').pop() || '').replace(/^\./, '').toLowerCase()
}

function fileCategory(item: LibraryItem): Exclude<LibraryPickerCategory, 'all'> {
  const extension = normalizedExtension(item)
  if (['xls', 'xlsx', 'xlsm', 'csv'].includes(extension)) return 'spreadsheet'
  if (['ppt', 'pptx'].includes(extension)) return 'presentation'
  if (extension === 'pdf') return 'pdf'
  return 'document'
}

function fileKindLabel(item: LibraryItem) {
  return categories.find((entry) => entry.value === fileCategory(item))?.label ?? '文档'
}

function iconFor(item: LibraryItem) {
  return resolveColoredFileIcon(item.name, item.mimeType).src ?? ''
}

function formatSize(bytes?: number | null) {
  if (!bytes) return '—'
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}
</script>

<style scoped>
.library-picker-modal { position: fixed; inset: 0; z-index: 1100; display: grid; padding: 24px; background: rgba(17, 17, 17, .28); backdrop-filter: blur(2px); place-items: center; }
.library-picker { box-sizing: border-box; width: min(580px, 100%); max-height: min(680px, calc(100vh - 48px)); padding: 22px; color: #171717; background: #fff; border: 1px solid #e8e8e8; border-radius: 16px; box-shadow: 0 24px 70px rgba(0, 0, 0, .15); }
.library-picker__header { display: flex; align-items: flex-start; justify-content: space-between; }
.library-picker__header h2 { margin: 0; font-size: 22px; font-weight: 600; }
.library-picker__header p { margin: 5px 0 0; color: #7b7b7b; font-size: 13px; }
.library-picker__header > button { width: 30px; height: 30px; color: #666; font-size: 24px; cursor: pointer; background: transparent; border: 0; }
.library-picker__header > button:disabled { cursor: wait; opacity: .45; }
.library-picker__search { display: flex; height: 38px; margin-top: 18px; padding: 0 11px; gap: 8px; align-items: center; background: #f7f7f7; border: 1px solid #e8e8e8; border-radius: 9px; }
.library-picker__search input { flex: 1; min-width: 0; background: transparent; border: 0; outline: 0; }
.library-picker__categories { display: flex; padding: 12px 0; gap: 4px; overflow-x: auto; border-bottom: 1px solid #ededed; }
.library-picker__categories button { padding: 6px 10px; color: #747474; white-space: nowrap; cursor: pointer; background: transparent; border: 0; border-radius: 7px; }
.library-picker__categories button.active { color: #171717; background: #f0f0f0; }
.library-picker__list { max-height: 360px; overflow: auto; }
.library-picker__row { display: grid; grid-template-columns: 45px minmax(0, 1fr) auto; width: 100%; padding: 11px 8px; gap: 12px; align-items: center; color: #171717; text-align: left; cursor: pointer; background: #fff; border: 0; border-bottom: 1px solid #f0f0f0; border-radius: 8px; }
.library-picker__row:hover:not(:disabled), .library-picker__row:focus-visible:not(:disabled) { background: #f7f7f6; outline: none; }
.library-picker__row:disabled { cursor: wait; opacity: .62; }
.library-picker__file-icon { display: grid; width: 42px; height: 38px; color: #767676; background: #f5f5f3; border: 1px solid #ededeb; border-radius: 9px; place-items: center; }
.library-picker__file-icon img { width: 23px; height: 23px; object-fit: contain; }
.library-picker__file-icon .n-icon { font-size: 21px; }
.library-picker__file-info { min-width: 0; }
.library-picker__file-info strong, .library-picker__file-info small { display: block; }
.library-picker__file-info strong { overflow: hidden; font-size: 14px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.library-picker__file-info small { margin-top: 3px; color: #8a8a8a; font-size: 12px; }
.library-picker__adding { color: #777; font-size: 12px; white-space: nowrap; }
.library-picker__state, .library-picker__empty { display: grid; min-height: 250px; gap: 10px; color: #777; place-content: center; place-items: center; }
.library-picker__state--error button { height: 34px; padding: 0 14px; color: #fff; cursor: pointer; background: #171717; border: 0; border-radius: 8px; }
.library-picker__empty strong { color: #333; }
.library-picker__empty p { max-width: 300px; margin: 0; text-align: center; }
</style>
