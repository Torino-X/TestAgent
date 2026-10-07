<template>
  <div class="empty-state">
    <div class="empty-state__hero">
      <div class="empty-state__icon">
        <n-icon :component="RocketOutline" :size="36" />
      </div>
      <h2>开始新的测试任务</h2>
      <p>上传需求文档、接口规范，或者直接描述你想测试的内容。</p>
    </div>

    <div class="suggestions">
      <button
        v-for="item in suggestions"
        :key="item.title"
        class="suggestion-card"
        type="button"
        @click="selectSuggestion(item.text)"
      >
        <span class="suggestion-card__icon" :style="{ color: item.color }">
          <n-icon :component="item.icon" />
        </span>
        <span class="suggestion-card__title">{{ item.title }}</span>
        <span class="suggestion-card__text">{{ item.text }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { NIcon } from 'naive-ui'
import { BugOutline, DocumentTextOutline, RocketOutline, ServerOutline, SpeedometerOutline } from '@vicons/ionicons5'

const emit = defineEmits<{
  select: [text: string]
}>()

const suggestions = [
  {
    icon: DocumentTextOutline,
    title: '测试方案生成',
    text: '帮我根据这份PRD需求文档和模板生成测试方案',
    color: '#1b1b1b'
  },
  {
    icon: ServerOutline,
    title: '测试用例生成',
    text: '根据我提供的文档生成完整的测试用例',
    color: '#f59e0b'
  },
  {
    icon: BugOutline,
    title: '缺陷分析报告生成',
    text: '根据我提供的缺陷分析汇总数据表生成缺陷分析报告',
    color: '#ba1a1a'
  },
  {
    icon: SpeedometerOutline,
    title: '需求文档分析',
    text: '根据我提供的需求文档，分析并提出需求建议',
    color: '#10b981'
  }
]

function selectSuggestion(text: string) {
  emit('select', text)
}
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 100%;
}

.empty-state__hero {
  width: min(100%, 680px);
  margin-bottom: 40px;
  text-align: center;
}

.empty-state__icon {
  display: grid;
  width: 80px;
  height: 80px;
  margin: 0 auto 20px;
  color: var(--ta-primary);
  place-items: center;
  background: var(--ta-surface);
  border: 1px solid rgba(229, 231, 235, 0.7);
  border-radius: var(--ta-radius-xl);
  box-shadow: var(--ta-shadow-soft);
}

.empty-state h2 {
  margin: 0 0 10px;
  color: var(--ta-text-strong);
  font-size: 32px;
  font-weight: 600;
  line-height: 40px;
  letter-spacing: 0;
}

.empty-state p {
  margin: 0;
  color: var(--ta-text-muted);
  font-size: 16px;
}

.suggestions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  width: min(100%, var(--ta-content-width));
}

.suggestion-card {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 8px;
  padding: 18px;
  text-align: left;
  cursor: pointer;
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-lg);
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease,
    transform 160ms ease;
}

.suggestion-card:hover {
  border-color: rgba(0, 0, 0, 0.28);
  box-shadow: var(--ta-shadow-soft);
  transform: translateY(-2px);
}

.suggestion-card__icon {
  display: inline-flex;
  align-items: center;
  padding-top: 1px;
}

.suggestion-card__title {
  color: var(--ta-text-strong);
  font-weight: 600;
}

.suggestion-card__text {
  grid-column: 1 / -1;
  color: var(--ta-text-muted);
  font-size: 13px;
}

@media (max-width: 760px) {
  .suggestions {
    grid-template-columns: 1fr;
  }

  .empty-state h2 {
    font-size: 24px;
    line-height: 32px;
  }
}
</style>
