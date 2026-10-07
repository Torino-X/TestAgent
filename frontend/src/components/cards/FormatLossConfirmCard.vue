<template>
  <section class="format-loss-card" aria-labelledby="format-loss-confirm-title">
    <header class="format-loss-card__header">
      <div class="format-loss-card__heading">
        <h3 id="format-loss-confirm-title">确认格式丢失处理</h3>
        <p>
          检测到导出文档有 {{ confirmation.lossCount }} 项格式丢失。
          {{ confirmation.summary || '请选择继续导出，或重新生成文档。' }}
        </p>
      </div>
      <span v-if="secondsRemaining !== null" class="format-loss-card__countdown">
        {{ secondsRemaining }}s
      </span>
    </header>

    <div class="format-loss-card__losses" aria-label="格式丢失明细">
      <div v-if="confirmation.losses.length === 0" class="format-loss-card__loss">
        <strong>格式检查</strong>
        <span>检测到用户可见的格式丢失，请确认后继续。</span>
      </div>
      <div
        v-for="(loss, index) in confirmation.losses"
        :key="`${loss.element}-${index}`"
        class="format-loss-card__loss"
      >
        <strong>{{ loss.element || '模板元素' }}</strong>
        <span>{{ loss.message || '该元素的格式可能影响文档呈现。' }}</span>
      </div>
    </div>

    <p v-if="error" class="format-loss-card__error" role="alert">{{ error }}</p>

    <footer class="format-loss-card__footer">
      <button
        v-for="choice in orderedChoices"
        :key="choice.id"
        type="button"
        class="format-loss-card__button"
        :class="`format-loss-card__button--${choice.id}`"
        :disabled="submitting"
        @click="emit('decision', choice.id)"
      >
        {{ submitting ? '正在提交...' : choice.label }}
      </button>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { FormatLossChoice, FormatLossConfirmation } from '@/types'

const props = withDefaults(
  defineProps<{
    confirmation: FormatLossConfirmation
    secondsRemaining?: number | null
    submitting?: boolean
    error?: string
  }>(),
  { secondsRemaining: null, submitting: false, error: '' }
)

const emit = defineEmits<{ decision: [decision: 'accept' | 'retry'] }>()

const fallbackChoices: FormatLossChoice[] = [
  { id: 'retry', label: '重新生成文档' },
  { id: 'accept', label: '接受格式丢失并继续' }
]

const orderedChoices = computed(() => {
  const choices = props.confirmation.choices.length ? props.confirmation.choices : fallbackChoices
  return [...choices].sort((left, right) => Number(left.id === 'accept') - Number(right.id === 'accept'))
})
</script>

<style scoped>
.format-loss-card { width: min(100%, 780px); overflow: hidden; background: #fff; border: 1px solid #d9d9d9; border-radius: 12px; box-shadow: 0 1px 3px rgba(15, 23, 42, .08); }
.format-loss-card__header { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; padding: 13px 12px 10px; }
.format-loss-card__heading { display: grid; min-width: 0; gap: 7px; }
.format-loss-card h3 { margin: 0; color: #191919; font-size: 14px; font-weight: 700; line-height: 1.45; }
.format-loss-card__heading p { margin: 0; color: #5f6368; font-size: 13px; line-height: 1.55; }
.format-loss-card__countdown { flex: 0 0 auto; padding: 3px 7px; color: #8a4b00; background: #fff3df; border-radius: 999px; font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.15; }
.format-loss-card__losses { display: grid; gap: 4px; padding: 0 12px; }
.format-loss-card__loss { display: grid; gap: 2px; padding: 8px 10px; color: #202124; background: #f4f4f4; border-radius: 5px; }
.format-loss-card__loss strong { font-size: 13px; font-weight: 600; line-height: 1.25; }
.format-loss-card__loss span { color: #71747a; font-size: 12px; line-height: 1.4; }
.format-loss-card__error { padding: 0 12px; margin: 8px 0 0; color: #b42318; font-size: 12px; }
.format-loss-card__footer { display: flex; flex-wrap: wrap; gap: 7px; justify-content: flex-end; padding: 12px; }
.format-loss-card__button { min-height: 25px; padding: 2px 9px; font: inherit; font-size: 12px; line-height: 1.2; cursor: pointer; border-radius: 5px; }
.format-loss-card__button--retry { color: #30343a; background: #fff; border: 1px solid #dfe1e5; }
.format-loss-card__button--accept { color: #fff; background: #0d0d0d; border: 1px solid #0d0d0d; font-weight: 600; }
.format-loss-card__button--retry:hover:not(:disabled) { background: #f3f4f6; }
.format-loss-card__button--accept:hover:not(:disabled) { background: #262626; border-color: #262626; }
.format-loss-card__button:disabled { cursor: not-allowed; opacity: .55; }
@media (max-width: 640px) { .format-loss-card__header { align-items: flex-start; } .format-loss-card__footer { justify-content: stretch; } .format-loss-card__button { flex: 1; } }
</style>
