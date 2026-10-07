<template>
  <Teleport to="body">
    <div v-if="show" class="move-dialog-mask" @click.self="emit('close')">
      <section class="move-dialog" role="dialog" aria-modal="true" aria-labelledby="move-dialog-title">
        <header><h2 id="move-dialog-title">移动到项目</h2><button type="button" aria-label="关闭" @click="emit('close')"><n-icon :component="CloseOutline" :size="20" /></button></header>
        <p>将“{{ conversation?.title }}”关联到一个项目。消息历史仍保留在当前会话中。</p>
        <div class="move-dialog__projects"><button v-for="project in projects" :key="project.id" type="button" :class="{ active: selectedId === project.id }" @click="selectedId = project.id"><n-icon :component="FolderOutline" :size="18" /><span>{{ project.name }}</span><small v-if="project.pinned">已置顶</small></button></div>
        <footer><button class="move-dialog__cancel" type="button" @click="emit('close')">取消</button><button class="move-dialog__submit" type="button" :disabled="!selectedId || saving" @click="move">移动</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { CloseOutline, FolderOutline } from '@vicons/ionicons5'
import type { Conversation } from '@/types'
import type { ProjectSummary } from '@/types/project'
const props = defineProps<{ show: boolean; conversation: Conversation | null; projects: ProjectSummary[]; saving?: boolean }>()
const emit = defineEmits<{ close: []; move: [projectId: string] }>()
const selectedId = ref('')
watch(() => props.show, (show) => { if (show) selectedId.value = props.projects[0]?.id ?? '' })
function move() { if (selectedId.value) emit('move', selectedId.value) }
</script>

<style scoped>
.move-dialog-mask { position: fixed; inset: 0; z-index: 1000; display: grid; padding: 24px; background: rgba(17,17,17,.34); backdrop-filter: blur(3px); place-items: center; }.move-dialog { width: min(460px, 100%); padding: 22px; color: #171717; background: #fff; border-radius: 18px; box-shadow: 0 26px 80px rgba(0,0,0,.22); }.move-dialog header { display: flex; justify-content: space-between; align-items: center; }.move-dialog h2 { margin: 0; font-size: 20px; font-weight: 600; }.move-dialog header button { display: grid; width: 34px; height: 34px; padding: 0; cursor: pointer; background: transparent; border: 0; border-radius: 50%; place-items: center; }.move-dialog > p { margin: 15px 0; color: #606060; font-size: 14px; line-height: 21px; }.move-dialog__projects { display: grid; gap: 5px; max-height: 236px; padding: 6px; overflow-y: auto; border: 1px solid #e6e6e6; border-radius: 12px; }.move-dialog__projects button { display: flex; min-height: 42px; gap: 9px; align-items: center; padding: 0 10px; color: #202020; font: inherit; font-size: 14px; text-align: left; cursor: pointer; background: transparent; border: 0; border-radius: 8px; }.move-dialog__projects button:hover, .move-dialog__projects button.active { background: #f1f1f1; }.move-dialog__projects span { flex: 1; }.move-dialog__projects small { padding: 2px 5px; color: #666; font-size: 11px; background: #e7e7e7; border-radius: 4px; }.move-dialog footer { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }.move-dialog footer button { height: 37px; padding: 0 16px; font: inherit; font-size: 14px; cursor: pointer; border-radius: 999px; }.move-dialog__cancel { background: #fff; border: 1px solid #dedede; }.move-dialog__submit { color: #fff; background: #171717; border: 1px solid #171717; }.move-dialog__submit:disabled { cursor: not-allowed; background: #c7c7c7; border-color: #c7c7c7; }
</style>
