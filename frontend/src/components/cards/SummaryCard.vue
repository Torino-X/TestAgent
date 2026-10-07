<template>
  <section class="summary-card ta-card">
    <header class="summary-card__header">
      <div class="summary-card__icon" :style="{ color, background: tint }">
        <n-icon :component="icon" />
      </div>
      <div>
        <h3>{{ summary.title }}</h3>
        <p>{{ summary.description }}</p>
      </div>
    </header>
    <div class="summary-card__metrics">
      <div v-for="metric in summary.metrics" :key="metric.label" class="metric">
        <span>{{ metric.value }}</span>
        <small>{{ metric.label }}</small>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { NIcon } from 'naive-ui'
import type { Component } from 'vue'
import type { SummaryCardData } from '@/types'

defineProps<{
  summary: SummaryCardData
  icon: Component
  color: string
  tint: string
}>()
</script>

<style scoped>
.summary-card {
  width: min(100%, var(--ta-content-width));
  padding: 16px;
}

.summary-card__header {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.summary-card__icon {
  display: grid;
  flex: 0 0 36px;
  width: 36px;
  height: 36px;
  place-items: center;
  border-radius: var(--ta-radius-md);
}

.summary-card h3 {
  margin: 0 0 4px;
  color: var(--ta-text-strong);
  font-size: var(--text-body-size);
  font-weight: var(--font-semibold);
  line-height: 24px;
}

.summary-card p {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--text-ui);
  line-height: var(--leading-ui);
}

.summary-card__metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 16px;
}

.metric {
  padding: 10px;
  background: var(--ta-bg-main);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-md);
}

.metric span,
.metric small {
  display: block;
}

.metric span {
  color: var(--ta-text-strong);
  font-size: 18px;
  font-weight: var(--font-semibold);
}

.metric small {
  color: var(--ta-text-muted);
  font-size: var(--text-meta);
  line-height: var(--leading-meta);
}
</style>
