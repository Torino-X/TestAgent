<template>
  <section class="review-card ta-card">
    <header class="review-card__header">
      <h3>审查结果</h3>
      <n-tag :type="review.passed ? 'success' : 'error'" :bordered="false">
        {{ review.passed ? '通过' : '未通过' }}
      </n-tag>
    </header>
    <div class="review-card__stats">
      <div><strong>{{ review.businessModules }}</strong><span>业务模块</span></div>
      <div><strong>{{ review.generatedSections }}</strong><span>生成章节</span></div>
      <div><strong>{{ review.keptSections }}</strong><span>保留章节</span></div>
      <div><strong>{{ review.manualSections.length }}</strong><span>人工补充</span></div>
    </div>
    <div class="review-card__risks">
      <p v-for="risk in review.risks" :key="risk">{{ risk }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { NTag } from 'naive-ui'
import type { ReviewResult } from '@/types'

defineProps<{
  review: ReviewResult
}>()
</script>

<style scoped>
.review-card {
  width: min(100%, var(--ta-content-width));
  padding: 16px;
}

.review-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.review-card h3 {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: var(--text-body-size);
  font-weight: var(--font-semibold);
  line-height: 24px;
}

.review-card__stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.review-card__stats div {
  padding: 10px;
  background: var(--ta-bg-main);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-md);
}

.review-card__stats strong,
.review-card__stats span {
  display: block;
}

.review-card__stats strong {
  color: var(--ta-text-strong);
  font-size: 20px;
  font-weight: var(--font-semibold);
}

.review-card__stats span,
.review-card__risks {
  color: var(--text-secondary);
  font-size: var(--text-ui);
  line-height: var(--leading-ui);
}

.review-card__risks {
  margin-top: 12px;
}

.review-card__risks p {
  margin: 4px 0 0;
}
</style>
