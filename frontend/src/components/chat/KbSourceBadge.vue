<template>
  <div class="kb-source-badge" :data-confidence="kb?.confidence ?? 'unknown'">
    <template v-if="kb?.hit && primarySource">
      <span class="kb-source-badge__icon">📚</span>
      <span class="kb-source-badge__label">来源：{{ primarySource }}</span>
      <span v-if="extraCount > 0" class="kb-source-badge__extra">+{{ extraCount }}</span>
      <span class="kb-source-badge__confidence">置信度：{{ confidenceLabel }}</span>
    </template>
    <template v-else-if="kb?.attempted && kb?.confidence === 'medium'">
      <span class="kb-source-badge__icon">📚</span>
      <span class="kb-source-badge__label">知识库参考（置信度偏低）</span>
    </template>
    <template v-else-if="kb?.attempted && !kb?.hit">
      <span class="kb-source-badge__icon">📚</span>
      <span class="kb-source-badge__label">知识库中暂无直接相关内容</span>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { KbDirectAnswer } from '@/types'

const props = defineProps<{
  kb: KbDirectAnswer
}>()

const sources = computed(() => props.kb?.sourceAttribution ?? [])

const primarySource = computed(() => {
  const first = sources.value[0]
  if (!first) return ''
  return first.doc_name || first.document_name || first.title || '知识库'
})

const extraCount = computed(() => Math.max(0, sources.value.length - 1))

const confidenceLabel = computed(() => {
  const conf = props.kb?.confidence
  if (conf === 'high') return '高'
  if (conf === 'medium') return '中'
  if (conf === 'low') return '低'
  return conf ?? '—'
})
</script>

<style scoped>
.kb-source-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  margin: 0 0 8px 0;
  background: rgba(56, 142, 234, 0.08);
  border: 1px solid rgba(56, 142, 234, 0.25);
  border-radius: 6px;
  font-size: 12px;
  color: #2a5d99;
  align-self: flex-start;
}

.kb-source-badge__icon {
  font-size: 14px;
}

.kb-source-badge__label {
  font-weight: 500;
}

.kb-source-badge__extra {
  color: #5a8ec0;
  font-size: 11px;
}

.kb-source-badge__confidence {
  color: #5a8ec0;
  font-size: 11px;
  margin-left: 4px;
}

.kb-source-badge[data-confidence='medium'] {
  background: rgba(234, 173, 56, 0.1);
  border-color: rgba(234, 173, 56, 0.3);
  color: #8a6817;
}

.kb-source-badge[data-confidence='low'],
.kb-source-badge[data-confidence='unknown'] {
  background: rgba(150, 150, 150, 0.1);
  border-color: rgba(150, 150, 150, 0.3);
  color: #555;
}
</style>