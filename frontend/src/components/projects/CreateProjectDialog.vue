<template>
  <Teleport to="body">
    <div v-if="show" class="project-dialog-mask" role="presentation" @click.self="close">
      <section class="project-dialog" role="dialog" aria-modal="true" aria-labelledby="create-project-title">
        <header class="project-dialog__header">
          <h2 id="create-project-title">创建项目</h2>
          <button class="project-dialog__close" type="button" aria-label="关闭" @click="close"><n-icon :component="CloseOutline" :size="20" /></button>
        </header>

        <label class="project-dialog__label" for="project-name">项目名称</label>
        <div class="project-dialog__input-wrap" :class="{ 'project-dialog__input-wrap--filled': trimmedName }">
          <n-icon :component="SparklesOutline" :size="17" />
          <input id="project-name" v-model="name" type="text" maxlength="120" placeholder="请输入项目名称" autofocus @keydown.enter="create" />
        </div>

        <div class="project-dialog__note">
          <n-icon :component="BulbOutline" :size="18" />
          <p>项目功能可将聊天、文件和自定义指令集中保存，以便用于持续进行的工作，或者单纯用于整理内容，让一切更井然有序。</p>
        </div>

        <div class="project-dialog__footer">
          <ProjectSelect v-model="memoryMode" :options="memoryOptions" aria-label="项目记忆选项" />
          <button class="project-dialog__submit" type="button" :disabled="!trimmedName || saving" @click="create">{{ saving ? '正在创建...' : '创建项目' }}</button>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { BulbOutline, CloseOutline, SparklesOutline } from '@vicons/ionicons5'
import ProjectSelect from './ProjectSelect.vue'
import type { AppSelectOption } from '@/components/common/appSelect'
import type { ProjectMemoryMode } from '@/types/project'

const props = withDefaults(defineProps<{ show: boolean; saving?: boolean }>(), { saving: false })
const emit = defineEmits<{ close: []; create: [payload: { name: string; memoryMode: ProjectMemoryMode }] }>()

const name = ref('')
const memoryMode = ref<ProjectMemoryMode>('project_memory')
const memoryOptions: AppSelectOption<string>[] = [
  { label: '默认记忆', value: 'project_memory', description: '此项目可以访问项目内聊天中的记忆，反之亦然。' },
  { label: '不使用项目记忆', value: 'none', description: '项目内聊天不会保存或共享项目记忆。' }
]
const trimmedName = computed(() => name.value.trim())

watch(() => props.show, (visible) => {
  if (visible) {
    name.value = ''
    memoryMode.value = 'project_memory'
  }
})

function close() { emit('close') }
function create() {
  if (!trimmedName.value || props.saving) return
  emit('create', { name: trimmedName.value, memoryMode: memoryMode.value })
}
</script>

<style scoped>
.project-dialog-mask { position: fixed; inset: 0; z-index: 1000; display: grid; padding: 24px; background: rgba(36, 36, 36, .16); backdrop-filter: blur(4px); place-items: center; }
.project-dialog { box-sizing: border-box; width: min(512px, 100%); padding: 16px; color: #171717; background: #fff; border-radius: 15px; box-shadow: 0 18px 46px rgba(20, 20, 20, .19); }
.project-dialog__header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 22px; }.project-dialog__header h2 { margin: 0; font-size: 18px; font-weight: 500; letter-spacing: -.2px; }.project-dialog__close { display: grid; width: 26px; height: 26px; padding: 0; color: #222; cursor: pointer; background: transparent; border: 0; border-radius: 7px; place-items: center; }.project-dialog__close:hover { background: #f1f1f1; }
.project-dialog__label { display: block; margin-bottom: 9px; color: #252525; font-size: 14px; font-weight: 400; }.project-dialog__input-wrap { display: flex; box-sizing: border-box; width: 100%; height: 36px; gap: 8px; align-items: center; padding: 0 10px; color: #9a9a9a; border: 1px solid #dedede; border-radius: 8px; transition: border-color .16s ease, box-shadow .16s ease; }.project-dialog__input-wrap:focus-within, .project-dialog__input-wrap--filled { color: #616161; border-color: #171717; }.project-dialog__input-wrap:focus-within { box-shadow: 0 0 0 1px #171717; }.project-dialog__input-wrap input { flex: 1; min-width: 0; height: 100%; padding: 0; color: #171717; font: inherit; font-size: 14px; outline: none; background: transparent; border: 0; }.project-dialog__input-wrap input::placeholder { color: #9b9b9b; }
.project-dialog__note { display: flex; min-height: 56px; box-sizing: border-box; gap: 9px; align-items: flex-start; margin-top: 16px; padding: 11px 12px; color: #666; background: #f4f4f4; border-radius: 11px; }.project-dialog__note :deep(.n-icon) { flex: 0 0 auto; margin-top: 3px; color: #646464; }.project-dialog__note p { margin: 0; font-size: 13px; line-height: 18px; }
.project-dialog__footer { display: flex; gap: 12px; align-items: center; justify-content: space-between; margin-top: 17px; }
.project-dialog__submit { height: 35px; padding: 0 15px; color: #fff; font: inherit; font-size: 14px; font-weight: 500; cursor: pointer; background: #171717; border: 1px solid #171717; border-radius: 999px; transition: background .16s ease, transform .16s ease; }.project-dialog__submit:hover:not(:disabled) { background: #303030; border-color: #303030; }.project-dialog__submit:active:not(:disabled) { transform: scale(.98); }.project-dialog__submit:disabled { cursor: not-allowed; background: #c7c7c7; border-color: #c7c7c7; }
@media (max-width: 560px) { .project-dialog-mask { padding: 16px; }.project-dialog { padding: 16px; }.project-dialog__footer { align-items: flex-start; flex-direction: column; }.project-dialog__submit { align-self: flex-end; } }
</style>
