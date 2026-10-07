<template>
  <Teleport to="body">
    <div v-if="show" class="source-picker-mask" @click.self="emit('close')">
      <section class="source-picker" role="dialog" aria-modal="true" aria-labelledby="source-picker-title">
        <header>
          <div><h2 id="source-picker-title">添加项目来源</h2><p>上传新文件，或关联资料库中的已有文件。</p></div>
          <button type="button" aria-label="关闭" @click="emit('close')"><n-icon :component="CloseOutline" :size="20" /></button>
        </header>

        <button class="source-picker__upload" type="button" :disabled="loading" @click="uploadInput?.click()">
          <n-icon :component="CloudUploadOutline" :size="19" />
          <span>上传新文件</span>
        </button>
        <input ref="uploadInput" class="source-picker__file" type="file" @change="selectUpload" />

        <div class="source-picker__separator"><span>资料库文件</span></div>
        <label class="source-picker__search">
          <n-icon :component="SearchOutline" :size="16" />
          <input v-model="query" type="search" placeholder="搜索资料库" />
        </label>

        <div v-if="libraryLoading" class="source-picker__state">正在加载资料库...</div>
        <div v-else-if="libraryError" class="source-picker__state source-picker__state--error">
          <span>{{ libraryError }}</span><button type="button" @click="loadLibrary">重试</button>
        </div>
        <div v-else-if="filteredItems.length" class="source-picker__list">
          <button v-for="item in filteredItems" :key="item.id" type="button" :disabled="loading" @click="emit('attach', item.id)">
            <n-icon :component="DocumentTextOutline" :size="19" />
            <span><strong>{{ item.name }}</strong><small>{{ item.extension || '文件' }}</small></span>
            <em>添加</em>
          </button>
        </div>
        <div v-else class="source-picker__state">资料库中还没有可关联的上传文件。</div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { CloseOutline, CloudUploadOutline, DocumentTextOutline, SearchOutline } from '@vicons/ionicons5'
import { fetchLibraryItems } from '@/api/libraryApi'
import type { LibraryItem } from '@/types/library'

const props = defineProps<{ show: boolean; loading?: boolean }>()
const emit = defineEmits<{ close: []; attach: [fileId: string]; upload: [file: File] }>()
const uploadInput = ref<HTMLInputElement | null>(null)
const query = ref('')
const libraryItems = ref<LibraryItem[]>([])
const libraryLoading = ref(false)
const libraryError = ref('')

const filteredItems = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase()
  return libraryItems.value.filter((item) => !keyword || item.name.toLocaleLowerCase().includes(keyword))
})

watch(() => props.show, (show) => {
  if (!show) return
  query.value = ''
  void loadLibrary()
})

async function loadLibrary() {
  libraryLoading.value = true
  libraryError.value = ''
  try {
    const response = await fetchLibraryItems('file', '', { source: 'upload', fileType: 'all' })
    libraryItems.value = response.items
  } catch (error) {
    libraryItems.value = []
    libraryError.value = error instanceof Error ? error.message : '资料库加载失败，请稍后重试。'
  } finally {
    libraryLoading.value = false
  }
}

function selectUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('upload', file)
  input.value = ''
}
</script>

<style scoped>
.source-picker-mask { position: fixed; inset: 0; z-index: 1000; display: grid; padding: 24px; background: rgba(17, 17, 17, .32); backdrop-filter: blur(3px); place-items: center; }
.source-picker { box-sizing: border-box; width: min(500px, 100%); padding: 20px; color: #171717; background: #fff; border: 1px solid #e3e3e3; border-radius: 17px; box-shadow: 0 22px 65px rgba(0, 0, 0, .18); }
.source-picker header { display: flex; align-items: flex-start; justify-content: space-between; }.source-picker h2 { margin: 0; font-size: 19px; font-weight: 550; }.source-picker header p { margin: 5px 0 0; color: #777; font-size: 13px; }.source-picker header > button { display: grid; width: 30px; height: 30px; padding: 0; cursor: pointer; background: transparent; border: 0; border-radius: 50%; place-items: center; }.source-picker header > button:hover { background: #f1f1f1; }
.source-picker__upload { display: flex; width: 100%; height: 43px; gap: 9px; align-items: center; justify-content: center; margin-top: 20px; color: #fff; font: inherit; font-size: 14px; cursor: pointer; background: #171717; border: 0; border-radius: 10px; }.source-picker__upload:disabled { cursor: wait; opacity: .55; }.source-picker__file { display: none; }
.source-picker__separator { display: flex; align-items: center; gap: 10px; margin: 18px 0 12px; color: #777; font-size: 12px; }.source-picker__separator::before, .source-picker__separator::after { flex: 1; height: 1px; content: ''; background: #ececec; }
.source-picker__search { display: flex; height: 36px; gap: 8px; align-items: center; padding: 0 10px; color: #858585; border: 1px solid #dedede; border-radius: 9px; }.source-picker__search input { flex: 1; min-width: 0; padding: 0; font: inherit; font-size: 14px; outline: 0; border: 0; }
.source-picker__list { display: grid; max-height: 245px; margin-top: 10px; overflow-y: auto; }.source-picker__list > button { display: flex; min-height: 49px; gap: 10px; align-items: center; padding: 0 9px; color: #222; font: inherit; cursor: pointer; background: transparent; border: 0; border-radius: 9px; }.source-picker__list > button:hover { background: #f4f4f4; }.source-picker__list > button:disabled { cursor: wait; opacity: .55; }.source-picker__list span { display: grid; flex: 1; min-width: 0; gap: 2px; text-align: left; }.source-picker__list strong { overflow: hidden; font-size: 14px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }.source-picker__list small { color: #888; font-size: 12px; }.source-picker__list em { color: #555; font-size: 13px; font-style: normal; }
.source-picker__state { display: flex; min-height: 100px; gap: 10px; align-items: center; justify-content: center; color: #777; font-size: 13px; text-align: center; }.source-picker__state--error { color: #b42318; }.source-picker__state button { padding: 5px 9px; color: #333; font: inherit; cursor: pointer; background: #fff; border: 1px solid #ddd; border-radius: 7px; }
@media (max-width: 560px) { .source-picker-mask { padding: 14px; }.source-picker { padding: 17px; } }
</style>
