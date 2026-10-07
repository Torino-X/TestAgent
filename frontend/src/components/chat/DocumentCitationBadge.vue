<template>
  <div v-if="citations.length" class="document-citations" aria-label="会话资料来源">
    <span class="document-citations__label">来自：</span>
    <span v-for="citation in citations" :key="citation.chunk_id" class="document-citations__item">
      {{ citation.title }}<template v-if="citation.section"> · {{ citation.section }}</template><template v-if="citation.version"> · v{{ citation.version }}</template>
    </span>
  </div>
</template>

<script setup lang="ts">
import type { DocumentCitation } from '@/types'

defineProps<{ citations: DocumentCitation[] }>()
</script>

<style scoped>
.document-citations {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  margin: 10px 0 0;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.5;
}

.document-citations__label {
  color: #4b5563;
}

.document-citations__item {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
