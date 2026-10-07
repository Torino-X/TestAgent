<template>
  <SummaryCard :summary="summary" :icon="AlbumsOutline" color="#4b41e1" tint="rgba(75,65,225,0.1)" />
</template>

<script setup lang="ts">
import { AlbumsOutline } from '@vicons/ionicons5'
import SummaryCard from './SummaryCard.vue'
import type { SummaryCardData } from '@/types'

defineProps<{
  summary: SummaryCardData
}>()
</script>
