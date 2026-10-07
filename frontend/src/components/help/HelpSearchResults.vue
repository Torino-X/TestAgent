<template>
  <section class="help-results" aria-live="polite" aria-label="搜索结果">
    <div v-if="loading" class="help-results__loading">
      <div v-for="index in 4" :key="index" class="help-results__skeleton"></div>
    </div>
    <div v-else-if="error" class="help-results__state">
      <n-icon :component="CloudOfflineOutline" :size="30" />
      <h2>搜索暂时不可用</h2>
      <p>{{ error }}</p>
      <button type="button" @click="$emit('retry')">重试</button>
    </div>
    <div v-else-if="query && !items.length" class="help-results__state">
      <n-icon :component="SearchOutline" :size="30" />
      <h2>没有找到相关内容</h2>
      <p>换个关键词，或从下方文档分类中浏览。</p>
    </div>
    <template v-else>
      <p v-if="query" class="help-results__count">找到 {{ items.length }} 篇相关内容</p>
      <RouterLink v-for="item in items" :key="item.slug" class="help-result" :to="`/help/article/${item.slug}`">
        <span class="help-result__section">{{ item.sectionTitle }}</span>
        <h2><template v-for="(part, index) in highlight(item.title)" :key="index"><mark v-if="part.match">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></h2>
        <p><template v-for="(part, index) in highlight(item.snippet || item.description)" :key="index"><mark v-if="part.match">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></p>
        <n-icon :component="ArrowForwardOutline" :size="18" aria-hidden="true" />
      </RouterLink>
    </template>
  </section>
</template>

<script setup lang="ts">
import { NIcon } from 'naive-ui'
import { ArrowForwardOutline, CloudOfflineOutline, SearchOutline } from '@vicons/ionicons5'
import type { HelpSearchItem } from '@/types/help'

const props = defineProps<{
  query: string
  items: HelpSearchItem[]
  loading: boolean
  error: string
}>()

defineEmits<{ retry: [] }>()

function highlight(value: string): Array<{ text: string; match: boolean }> {
  const query = props.query.trim()
  if (!query) return [{ text: value, match: false }]
  const lowerValue = value.toLocaleLowerCase()
  const lowerQuery = query.toLocaleLowerCase()
  const parts: Array<{ text: string; match: boolean }> = []
  let cursor = 0
  let position = lowerValue.indexOf(lowerQuery)
  while (position >= 0) {
    if (position > cursor) parts.push({ text: value.slice(cursor, position), match: false })
    parts.push({ text: value.slice(position, position + query.length), match: true })
    cursor = position + query.length
    position = lowerValue.indexOf(lowerQuery, cursor)
  }
  if (cursor < value.length) parts.push({ text: value.slice(cursor), match: false })
  return parts.length ? parts : [{ text: value, match: false }]
}
</script>

<style scoped>
.help-results { display: grid; gap: 10px; }
.help-results__count { margin: 0 0 6px; color: #6b7280; font-size: 13px; }
.help-result {
  position: relative;
  display: block;
  padding: 18px 54px 18px 20px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  transition: border-color 160ms ease, transform 160ms ease, box-shadow 160ms ease;
}
.help-result:hover { transform: translateY(-1px); border-color: #cfd2d7; box-shadow: 0 8px 22px rgba(17, 24, 39, .05); }
.help-result__section { color: #787f8a; font-size: 12px; font-weight: 600; }
.help-result h2 { margin: 5px 0 4px; color: #111827; font-size: 16px; line-height: 24px; }
.help-result p { margin: 0; color: #6b7280; font-size: 14px; line-height: 22px; }
.help-result > .n-icon { position: absolute; top: 50%; right: 20px; color: #9ca3af; transform: translateY(-50%); }
.help-result mark { padding: 0 2px; color: inherit; background: #fff0b8; border-radius: 2px; }
.help-results__state { padding: 56px 24px; color: #8b9099; text-align: center; background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; }
.help-results__state h2 { margin: 12px 0 5px; color: #272a2f; font-size: 17px; }
.help-results__state p { margin: 0; font-size: 14px; }
.help-results__state button { margin-top: 16px; padding: 8px 18px; color: #fff; cursor: pointer; background: #111; border: 0; border-radius: 8px; }
.help-results__loading { display: grid; gap: 10px; }
.help-results__skeleton { height: 104px; background: linear-gradient(100deg, #f3f4f6 30%, #fafafa 45%, #f3f4f6 60%); background-size: 250% 100%; border-radius: 12px; animation: help-shimmer 1.2s infinite; }
@keyframes help-shimmer { to { background-position-x: -250%; } }
</style>
