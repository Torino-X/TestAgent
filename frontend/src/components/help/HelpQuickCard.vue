<template>
  <RouterLink class="help-quick-card" :to="`/help/article/${article.slug}`">
    <span class="help-quick-card__number">0{{ index + 1 }}</span>
    <div>
      <h3>{{ article.title }}</h3>
      <p>{{ article.description }}</p>
    </div>
    <n-icon :component="ArrowForwardOutline" :size="19" aria-hidden="true" />
  </RouterLink>
</template>

<script setup lang="ts">
import { NIcon } from 'naive-ui'
import { ArrowForwardOutline } from '@vicons/ionicons5'
import type { HelpArticleSummary } from '@/types/help'

defineProps<{ article: HelpArticleSummary; index: number }>()
</script>

<style scoped>
.help-quick-card {
  display: grid;
  grid-template-columns: auto 1fr auto;
  min-height: 154px;
  padding: 24px;
  gap: 17px;
  align-items: start;
  background: #fff;
  border: 1px solid #e3e5e8;
  border-radius: 14px;
  transition: transform 170ms ease, box-shadow 170ms ease, border-color 170ms ease;
}
.help-quick-card:hover { transform: translateY(-2px); border-color: #cdd0d5; box-shadow: 0 12px 30px rgba(17, 24, 39, .06); }
.help-quick-card__number { display: grid; width: 32px; height: 32px; color: #fff; font-size: 12px; font-weight: 700; background: #171717; border-radius: 9px; place-items: center; }
.help-quick-card h3 { margin: 3px 0 8px; color: #111827; font-size: 17px; line-height: 24px; }
.help-quick-card p { margin: 0; color: #707680; font-size: 13px; line-height: 21px; }
.help-quick-card > .n-icon { margin-top: 7px; color: #9ca3af; }
</style>
