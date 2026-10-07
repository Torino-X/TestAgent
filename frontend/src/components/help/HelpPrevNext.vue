<template>
  <nav class="help-prev-next" aria-label="相邻文章">
    <RouterLink v-if="previous" :to="`/help/article/${previous.slug}`">
      <span>上一篇</span><strong>{{ previous.title }}</strong>
    </RouterLink>
    <span v-else></span>
    <RouterLink v-if="next" class="help-prev-next__next" :to="`/help/article/${next.slug}`">
      <span>下一篇</span><strong>{{ next.title }}</strong>
    </RouterLink>
  </nav>
</template>

<script setup lang="ts">
import type { HelpAdjacentArticle } from '@/types/help'
defineProps<{ previous: HelpAdjacentArticle | null; next: HelpAdjacentArticle | null }>()
</script>

<style scoped>
.help-prev-next { display: grid; grid-template-columns: 1fr 1fr; margin-top: 56px; padding-top: 24px; gap: 14px; border-top: 1px solid #e5e7eb; }
.help-prev-next a { min-width: 0; padding: 15px 17px; background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; }
.help-prev-next a:hover { border-color: #c9cdd2; }
.help-prev-next span { display: block; margin-bottom: 4px; color: #959aa2; font-size: 11px; }
.help-prev-next strong { display: block; overflow: hidden; color: #20242a; font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.help-prev-next__next { text-align: right; }
@media (max-width: 640px) { .help-prev-next { grid-template-columns: 1fr; } }
</style>
