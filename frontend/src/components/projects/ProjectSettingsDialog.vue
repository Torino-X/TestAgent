<template>
  <Teleport to="body">
    <div v-if="show" class="project-settings-mask" @click.self="emit('close')">
      <section class="project-settings" role="dialog" aria-modal="true" aria-labelledby="project-settings-title">
        <header class="project-settings__header">
          <h2 id="project-settings-title">项目设置</h2>
          <button type="button" aria-label="关闭" @click="emit('close')"><n-icon :component="CloseOutline" :size="19" /></button>
        </header>
        <div class="project-settings__field">
          <label for="project-settings-name">项目名称</label>
          <input id="project-settings-name" v-model="name" type="text" maxlength="120" />
        </div>
        <div class="project-settings__field">
          <label for="project-settings-instructions">项目指令</label>
          <p>用于补充项目背景和协作约束。项目文件将由项目上下文独立检索。</p>
          <textarea id="project-settings-instructions" v-model="instructions" rows="3" placeholder="例如：优先使用项目资料；不确定的需求需要明确标注待确认项。" />
        </div>
        <div class="project-settings__field">
          <label for="project-settings-memory">项目记忆</label>
          <ProjectSelect id="project-settings-memory" v-model="memoryMode" :options="memoryOptions" aria-label="项目记忆选项" block />
          <p>该项目可访问项目内对话产生的记忆。</p>
        </div>
        <button class="project-settings__delete" type="button" :disabled="saving" @click="emit('delete')">删除项目</button>
        <footer class="project-settings__footer">
          <button class="project-settings__cancel" type="button" @click="emit('close')">取消</button>
          <button class="project-settings__save" type="button" :disabled="!name.trim() || saving" @click="save">保存更改</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { CloseOutline } from '@vicons/ionicons5'
import ProjectSelect from './ProjectSelect.vue'
import type { AppSelectOption } from '@/components/common/appSelect'
import type { ProjectDetail, ProjectMemoryMode } from '@/types/project'

const props = defineProps<{ show: boolean; project: ProjectDetail | null; saving?: boolean }>()
const emit = defineEmits<{
  close: []
  delete: []
  save: [payload: { name: string; description: string; memoryMode: ProjectMemoryMode; instructions: string }]
}>()
const name = ref('')
const description = ref('')
const memoryMode = ref<ProjectMemoryMode>('project_memory')
const instructions = ref('')
const memoryOptions: AppSelectOption<string>[] = [
  { label: '默认记忆', value: 'project_memory', description: '项目内对话产生的记忆可在该项目中使用。' },
  { label: '不保存项目记忆', value: 'none', description: '项目内对话不保存或共享项目记忆。' }
]

watch(() => [props.show, props.project] as const, () => {
  if (!props.show || !props.project) return
  name.value = props.project.name
  description.value = props.project.description
  memoryMode.value = props.project.memoryMode
  instructions.value = props.project.instructions
}, { immediate: true })

function save() {
  if (!name.value.trim()) return
  emit('save', { name: name.value.trim(), description: description.value.trim(), memoryMode: memoryMode.value, instructions: instructions.value.trim() })
}
</script>

<style scoped>
.project-settings-mask { position: fixed; inset: 0; z-index: 1000; display: grid; padding: 24px; background: rgba(17, 17, 17, .36); backdrop-filter: blur(3px); place-items: center; }
.project-settings { box-sizing: border-box; width: min(514px, 100%); padding: 18px 16px 16px; color: #171717; background: #fff; border: 1px solid rgba(17, 17, 17, .14); border-radius: 16px; box-shadow: 0 22px 65px rgba(0, 0, 0, .22); }.project-settings__header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; }.project-settings__header h2 { margin: 0; font-size: 18px; font-weight: 500; }.project-settings__header button { display: grid; width: 32px; height: 32px; padding: 0; color: #202020; cursor: pointer; background: transparent; border: 0; border-radius: 50%; place-items: center; }.project-settings__header button:hover { background: #f1f1f1; }.project-settings__field { display: grid; gap: 8px; margin-top: 20px; }.project-settings__field:first-of-type { margin-top: 0; }.project-settings__field > label { color: #242424; font-size: 14px; font-weight: 500; }.project-settings__field > p { margin: -2px 0 0; color: #8a8a8a; font-size: 13px; line-height: 19px; }.project-settings input, .project-settings textarea { box-sizing: border-box; width: 100%; color: #1d1d1f; font: inherit; font-size: 14px; background: #fff; border: 1px solid #d8d8d8; border-radius: 8px; outline: none; }.project-settings input { height: 38px; padding: 0 12px; }.project-settings textarea { min-height: 88px; padding: 10px 12px; line-height: 20px; resize: vertical; }.project-settings input:focus, .project-settings textarea:focus { border-color: #6a6a6a; box-shadow: 0 0 0 1px #6a6a6a; }.project-settings__delete { height: 36px; margin-top: 28px; padding: 0 13px; color: #e32d42; font: inherit; font-size: 14px; cursor: pointer; background: #fff; border: 1px solid #ff3952; border-radius: 999px; }.project-settings__footer { display: flex; gap: 10px; justify-content: flex-end; margin-top: 24px; padding-top: 16px; border-top: 1px solid #ededed; }.project-settings__footer button { height: 36px; padding: 0 15px; font: inherit; font-size: 14px; cursor: pointer; border-radius: 999px; }.project-settings__cancel { color: #222; background: #fff; border: 1px solid #dedede; }.project-settings__save { color: #fff; background: #171717; border: 1px solid #171717; }.project-settings__save:disabled { cursor: not-allowed; background: #b8b8b8; border-color: #b8b8b8; }
</style>
