<template>
  <SummaryCard :summary="summary" :icon="DocumentTextOutline" color="#1b1b1b" tint="#f3f4f6" />
</template>

<script setup lang="ts">
import { DocumentTextOutline } from '@vicons/ionicons5'
import SummaryCard from './SummaryCard.vue'
import type { SummaryCardData } from '@/types'

defineProps<{
  summary: SummaryCardData
}>()
</script>
