<template>
  <nav class="help-category-nav" aria-label="帮助文档目录">
    <RouterLink class="help-category-nav__home" to="/help">
      <n-icon :component="HomeOutline" :size="16" />
      帮助首页
    </RouterLink>
    <section v-for="section in sections" :key="section.id">
      <h2>{{ section.title }}</h2>
      <RouterLink
        v-for="article in section.articles"
        :key="article.slug"
        :class="{ active: article.slug === activeSlug }"
        :to="`/help/article/${article.slug}`"
        @click="$emit('navigate')"
      >{{ article.title }}</RouterLink>
    </section>
  </nav>
</template>

<script setup lang="ts">
import { NIcon } from 'naive-ui'
import { HomeOutline } from '@vicons/ionicons5'
import type { HelpSection } from '@/types/help'

defineProps<{ sections: HelpSection[]; activeSlug: string }>()
defineEmits<{ navigate: [] }>()
</script>

<style scoped>
.help-category-nav { padding: 24px 18px 40px; }
.help-category-nav__home { display: flex; align-items: center; height: 36px; padding: 0 10px; gap: 8px; color: #505660; font-size: 13px; border-radius: 7px; }
.help-category-nav__home:hover { background: #f3f4f6; }
.help-category-nav section { margin-top: 25px; }
.help-category-nav h2 { margin: 0 0 9px; padding: 0 10px; color: #24272c; font-size: 15px; font-weight: 700; line-height: 22px; letter-spacing: .015em; text-transform: uppercase; }
.help-category-nav section a { display: block; margin: 1px 0; padding: 7px 10px; color: #5f6670; font-size: 13px; line-height: 19px; border-radius: 7px; }
.help-category-nav section a:hover { color: #111827; background: #f5f5f5; }
.help-category-nav section a.active { color: #111827; font-weight: 600; background: #eceeef; }
</style>
