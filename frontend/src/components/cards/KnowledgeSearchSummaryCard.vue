<template>
  <SummaryCard :summary="summary" :icon="BookOutline" color="#10b981" tint="rgba(16,185,129,0.1)" />
</template>

<script setup lang="ts">
import { BookOutline } from '@vicons/ionicons5'
import SummaryCard from './SummaryCard.vue'
import type { SummaryCardData } from '@/types'

defineProps<{
  summary: SummaryCardData
}>()
</script>
