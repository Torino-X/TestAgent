<template>
  <section class="plan-card ta-card">
    <header class="plan-card__header">
      <div class="plan-card__title">
        <n-icon :component="AnalyticsOutline" />
        <span>执行计划</span>
      </div>
      <span class="plan-card__progress">{{ doneCount }} / {{ steps.length }} 完成</span>
    </header>
    <ol class="plan-card__list">
      <li v-for="(step, index) in steps" :key="step.id" class="plan-step" :class="`plan-step--${step.status}`">
        <span class="plan-step__marker">
          <n-icon v-if="step.status === 'done'" :component="Checkmark" :size="12" />
        </span>
        <span class="plan-step__body">
          <span class="plan-step__title">{{ index + 1 }}. {{ step.title }}</span>
          <span v-if="step.detail" class="plan-step__detail">{{ step.detail }}</span>
        </span>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NIcon } from 'naive-ui'
import { AnalyticsOutline, Checkmark } from '@vicons/ionicons5'
import type { TaskPlanStep } from '@/types'

const props = defineProps<{
  steps: TaskPlanStep[]
}>()

const doneCount = computed(() => props.steps.filter((step) => step.status === 'done').length)
</script>

<style scoped>
.plan-card {
  width: min(100%, var(--ta-content-width));
  overflow: hidden;
}

.plan-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  background: var(--ta-surface-low);
  border-bottom: 1px solid var(--ta-border);
}

.plan-card__title {
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--ta-text-strong);
  font-size: var(--text-md);
  font-weight: var(--font-semibold);
  line-height: var(--leading-ui);
}

.plan-card__progress {
  padding: 4px 8px;
  color: var(--ta-text-muted);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  line-height: var(--leading-xs);
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-sm);
}

.plan-card__list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px;
  margin: 0;
  list-style: none;
}

.plan-step {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.plan-step__marker {
  display: grid;
  flex: 0 0 16px;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  color: #ffffff;
  place-items: center;
  background: var(--ta-surface-variant);
  border-radius: 999px;
}

.plan-step--done .plan-step__marker {
  background: var(--ta-success);
}

.plan-step--running {
  padding: 12px;
  margin: 0 -8px;
  background: var(--ta-surface-low);
  border: 1px solid #d1d5db;
  border-radius: var(--ta-radius-md);
}

.plan-step--running .plan-step__marker {
  width: 12px;
  height: 12px;
  margin: 4px 2px 0;
  background: var(--ta-running);
  animation: ta-pulse 2s infinite;
}

.plan-step--done .plan-step__title {
  color: var(--ta-text-muted);
  text-decoration: line-through;
}

.plan-step--pending {
  opacity: 0.5;
}

.plan-step__body {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.plan-step__title {
  color: var(--ta-text);
  font-size: var(--text-ui);
  font-weight: var(--font-medium);
  line-height: var(--leading-ui);
}

.plan-step__detail {
  color: var(--text-secondary);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
}
</style>
