<template>
  <section class="artifact-card ta-card artifact-card--previewable" role="button" tabindex="0" @click="openPreview" @keyup.enter="openPreview">
    <div class="artifact-card__icon" :class="{ 'artifact-card__icon--asset': hasAssetIcon }">
      <img
        v-if="hasAssetIcon"
        :src="assetIconSrc"
        :alt="`${artifact.name} 文件类型图标`"
        class="artifact-card__icon-image"
      />
      <n-icon v-else :component="DocumentTextOutline" />
    </div>
    <div class="artifact-card__body">
      <h3>{{ artifact.name }}</h3>
      <p>.docx · {{ artifact.size }} · {{ artifact.generatedAt }}</p>
    </div>
    <div class="artifact-card__actions">
      <n-button secondary @click.stop>
        <template #icon>
          <n-icon :component="RefreshOutline" />
        </template>
        继续修改
      </n-button>
      <n-button type="primary" :loading="downloading" @click.stop="$emit('download', artifact.id, artifact.name)">
        <template #icon>
          <n-icon :component="DownloadOutline" />
        </template>
        下载产物
      </n-button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NIcon } from 'naive-ui'
import { DocumentTextOutline, DownloadOutline, RefreshOutline } from '@vicons/ionicons5'
import type { Artifact } from '@/types'
import { resolveFileIcon, isAssetIcon } from '@/utils/fileIcon'

const props = defineProps<{
  artifact: Artifact
  downloading?: boolean
  conversationId?: string
  conversationTitle?: string
}>()

defineEmits<{
  download: [artifactId: string, fallbackName?: string]
}>()

const resolvedIcon = resolveFileIcon(props.artifact.name)
const router = useRouter()
const hasAssetIcon = computed(() => isAssetIcon(resolvedIcon))
const assetIconSrc = computed(() => resolvedIcon.src ?? '')
function openPreview() {
  router.push({
    name: 'document-preview',
    params: { itemId: props.artifact.id },
    query: props.conversationId ? { from: 'chat', conversationId: props.conversationId, conversationTitle: props.conversationTitle || '对话' } : { from: 'library' }
  })
}
</script>

<style scoped>
.artifact-card {
  display: flex;
  gap: 14px;
  align-items: center;
  width: min(100%, var(--ta-content-width));
  padding: 16px;
}
.artifact-card--previewable { cursor: pointer; }.artifact-card--previewable:focus-visible { outline: 2px solid var(--ta-primary); outline-offset: 3px; }

.artifact-card__icon {
  display: grid;
  width: 48px;
  height: 48px;
  color: var(--ta-primary);
  place-items: center;
  background: var(--ta-primary-fixed);
  border-radius: var(--ta-radius-md);
}

.artifact-card__icon-image {
  width: 32px;
  height: 32px;
  object-fit: contain;
}

.artifact-card__body {
  flex: 1;
  min-width: 0;
}

.artifact-card h3,
.artifact-card p {
  margin: 0;
}

.artifact-card h3 {
  overflow: hidden;
  color: var(--ta-text-strong);
  font-size: var(--text-body-size);
  font-weight: var(--font-semibold);
  line-height: 24px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-card p {
  margin-top: 4px;
  color: var(--text-tertiary);
  font-size: var(--text-meta);
  line-height: var(--leading-meta);
}

.artifact-card__actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;  /* BUG FIX 2026-08-18: 防止长中文文件名把按钮挤压或侧向溢出 */
}

@media (max-width: 760px) {
  .artifact-card {
    align-items: flex-start;
    flex-direction: column;
  }

  .artifact-card__actions {
    width: 100%;
  }
}
</style>
