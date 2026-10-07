<template>
  <nav v-if="headings.length" class="help-toc" aria-label="本页目录">
    <h2>本页目录</h2>
    <a
      v-for="heading in headings"
      :key="heading.anchor"
      :class="{ active: heading.anchor === activeAnchor, nested: heading.level === 3 }"
      :href="`#${heading.anchor}`"
      @click.prevent="scrollTo(heading.anchor)"
    >{{ heading.text }}</a>
  </nav>
</template>

<script setup lang="ts">
import type { HelpHeading } from '@/types/help'

defineProps<{ headings: HelpHeading[]; activeAnchor: string }>()
const emit = defineEmits<{ select: [anchor: string] }>()

function scrollTo(anchor: string) {
  document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  history.replaceState(null, '', `#${encodeURIComponent(anchor)}`)
  emit('select', anchor)
}
</script>

<style scoped>
.help-toc { padding: 4px 0 24px 22px; border-left: 1px solid #e5e7eb; }
.help-toc h2 { margin: 0 0 12px; color: #3f444b; font-size: 12px; font-weight: 700; }
.help-toc a { position: relative; display: block; padding: 5px 8px 5px 0; color: #858b94; font-size: 12px; line-height: 18px; }
.help-toc a.nested { padding-left: 12px; }
.help-toc a:hover, .help-toc a.active { color: #111827; }
.help-toc a.active::before { position: absolute; top: 6px; bottom: 6px; left: -23px; width: 2px; content: ''; background: #111827; border-radius: 2px; }
</style>
