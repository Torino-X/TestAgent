<template>
  <section class="generating-card ta-card">
    <div class="generating-card__pulse"></div>
    <div class="generating-card__body">
      <h3>{{ status.title }}</h3>
      <p>{{ status.currentStep }} · {{ status.currentSection }}</p>
      <n-progress type="line" :percentage="status.progress" :height="8" :show-indicator="false" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { NProgress } from 'naive-ui'
import type { GeneratingStatus } from '@/types'

defineProps<{
  status: GeneratingStatus
}>()
</script>

<style scoped>
.generating-card {
  display: flex;
  gap: 14px;
  align-items: center;
  width: min(100%, var(--ta-content-width));
  padding: 16px;
}

.generating-card__pulse {
  width: 14px;
  height: 14px;
  background: var(--ta-running);
  border-radius: 999px;
  animation: ta-pulse 2s infinite;
}

.generating-card__body {
  flex: 1;
  min-width: 0;
}

.generating-card h3,
.generating-card p {
  margin: 0;
}

.generating-card h3 {
  color: var(--ta-text-strong);
}

.generating-card p {
  margin: 4px 0 12px;
  color: var(--ta-text-muted);
}
</style>
