<template>
  <section
    class="clarification-card"
    :class="{ 'clarification-card--collapsed': collapsed }"
    aria-labelledby="clarification-title"
  >
    <button
      v-if="collapsed"
      type="button"
      class="clarification-card__restore"
      @click="setCollapsed(false)"
    >
      继续补充关键信息
    </button>
    <template v-else>
      <header class="clarification-card__header">
        <div class="clarification-card__heading">
          <span class="clarification-card__progress">{{ currentIndex + 1 }}/{{ cards.length }}</span>
          <h3 id="clarification-title">
            {{ activeCard.question }}
            <small v-if="isMultiple">（可多选）</small>
          </h3>
        </div>
        <div class="clarification-card__header-actions">
          <button type="button" aria-label="收起补充信息" @click="setCollapsed(true)">
            <n-icon :component="ChevronDownOutline" />
          </button>
          <button type="button" aria-label="暂时收起补充信息" @click="setCollapsed(true)">
            <n-icon :component="CloseOutline" />
          </button>
        </div>
      </header>

      <div class="clarification-card__options" :aria-label="activeCard.question">
        <button
          v-for="option in activeOptions"
          :key="option.id"
          type="button"
          class="clarification-option"
          :class="{ 'clarification-option--selected': isSelected(option.id) }"
          :aria-pressed="isSelected(option.id)"
          :disabled="submitting"
          @click="toggleSelection(option.id)"
        >
          <span class="clarification-option__copy">
            <strong>{{ option.label }}</strong>
            <small v-if="option.description">{{ option.description }}</small>
          </span>
          <span class="clarification-option__indicator" aria-hidden="true">
            {{ isSelected(option.id) ? '✓' : '' }}
          </span>
        </button>

        <div class="clarification-option clarification-option--other" :class="{ 'clarification-option--selected': isSelected(otherOptionId) }">
          <button
            type="button"
            class="clarification-option__other-trigger"
            :aria-pressed="isSelected(otherOptionId)"
            :disabled="submitting"
            @click="toggleSelection(otherOptionId)"
          >
            <span class="clarification-option__copy"><strong>其他</strong></span>
            <span class="clarification-option__indicator" aria-hidden="true">
              {{ isSelected(otherOptionId) ? '✓' : '' }}
            </span>
          </button>
          <input
            v-if="isSelected(otherOptionId)"
            v-model="otherAnswers[activeCard.id]"
            :disabled="submitting"
            class="clarification-option__other-input"
            type="text"
            placeholder="请输入你的补充说明"
            @input="validationError = ''"
          />
        </div>
      </div>

      <p v-if="validationError" class="clarification-card__error" role="alert">{{ validationError }}</p>

      <footer class="clarification-card__footer">
        <button
          v-if="currentIndex > 0"
          type="button"
          class="clarification-card__button clarification-card__button--secondary"
          :disabled="submitting"
          @click="goBack"
        >
          Back
        </button>
        <span class="clarification-card__footer-spacer"></span>
        <button
          type="button"
          class="clarification-card__button clarification-card__button--secondary"
          :disabled="submitting"
          @click="skipCurrent"
        >
          Skip
        </button>
        <button
          type="button"
          class="clarification-card__button clarification-card__button--primary"
          :disabled="submitting || !canAdvance"
          @click="advance"
        >
          {{ submitting ? '提交中…' : isLastCard ? '提交' : 'Next' }}
        </button>
      </footer>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { NIcon } from 'naive-ui'
import { ChevronDownOutline, CloseOutline } from '@vicons/ionicons5'
import type { PreparationClarification } from '@/types'

type ClarificationOption = NonNullable<PreparationClarification['cards'][number]['options']>[number]

const props = defineProps<{
  clarification: PreparationClarification
  submitting?: boolean
}>()

const emit = defineEmits<{
  submit: [payload: { answers: Record<string, string>; conservativeGapIds: string[] }]
  'layout-change': [expanded: boolean]
}>()

const otherOptionId = '__other__'
const conservativeOptionId = '__conservative_scope__'
const fallbackOptions: ClarificationOption[] = [
  { id: 'confirm_current_scope', label: '按当前需求描述执行', description: '将已明确的范围作为本次测试基线。' },
  { id: 'not_applicable', label: '本项不适用于本次试运行', description: '明确排除该规则，并在方案中记录原因。' }
]
const currentIndex = ref(0)
const selectedValues = ref<Record<string, string[]>>({})
const otherAnswers = ref<Record<string, string>>({})
const skippedIds = ref<string[]>([])
const validationError = ref('')
const collapsed = ref(false)

const cards = computed(() => props.clarification.cards)
const activeCard = computed(() => cards.value[currentIndex.value] ?? cards.value[0])
const isMultiple = computed(() => activeCard.value?.selectionMode === 'multiple')
const isLastCard = computed(() => currentIndex.value === cards.value.length - 1)
const activeOptions = computed<ClarificationOption[]>(() => {
  const card = activeCard.value
  if (!card) return []
  const options = card.options?.length ? card.options : fallbackOptions
  return card.allowConservativeScope
    ? [...options, { id: conservativeOptionId, label: '按保守范围生成', description: '明确采用最小可验证范围，不补充该项的业务规则。' }]
    : options
})
const canAdvance = computed(() => {
  const card = activeCard.value
  if (!card || skippedIds.value.includes(card.id)) return true
  const selected = selectedValues.value[card.id] ?? []
  return selected.length > 0 && (!selected.includes(otherOptionId) || Boolean(otherAnswers.value[card.id]?.trim()))
})

function isSelected(optionId: string): boolean {
  return Boolean(activeCard.value && selectedValues.value[activeCard.value.id]?.includes(optionId))
}

function setCollapsed(nextCollapsed: boolean) {
  collapsed.value = nextCollapsed
  emit('layout-change', !nextCollapsed)
}

function toggleSelection(optionId: string) {
  const card = activeCard.value
  if (!card || props.submitting) return
  const selected = selectedValues.value[card.id] ?? []
  const next = isMultiple.value
    ? (selected.includes(optionId) ? selected.filter((id) => id !== optionId) : [...selected, optionId])
    : (selected.includes(optionId) ? [] : [optionId])
  selectedValues.value = { ...selectedValues.value, [card.id]: next }
  skippedIds.value = skippedIds.value.filter((id) => id !== card.id)
  if (!next.includes(otherOptionId)) otherAnswers.value = { ...otherAnswers.value, [card.id]: '' }
  validationError.value = ''
}

function goBack() {
  currentIndex.value = Math.max(0, currentIndex.value - 1)
  validationError.value = ''
}

function skipCurrent() {
  const card = activeCard.value
  if (!card || props.submitting) return
  skippedIds.value = [...new Set([...skippedIds.value, card.id])]
  selectedValues.value = { ...selectedValues.value, [card.id]: [] }
  otherAnswers.value = { ...otherAnswers.value, [card.id]: '' }
  validationError.value = ''
  if (isLastCard.value) submit()
  else currentIndex.value += 1
}

function advance() {
  if (!canAdvance.value || props.submitting) return
  validationError.value = ''
  if (isLastCard.value) submit()
  else currentIndex.value += 1
}

function submit() {
  const answers: Record<string, string> = {}
  const conservativeGapIds: string[] = []
  for (const card of cards.value) {
    if (skippedIds.value.includes(card.id)) {
      answers[card.id] = '【已跳过】用户暂未确认此项。'
      continue
    }
    const selected = selectedValues.value[card.id] ?? []
    const labels = selected
      .filter((id) => id !== otherOptionId && id !== conservativeOptionId)
      .map((id) => activeOptionsFor(card).find((option) => option.id === id)?.label)
      .filter((label): label is string => Boolean(label))
    if (selected.includes(conservativeOptionId)) conservativeGapIds.push(card.id)
    const other = selected.includes(otherOptionId) ? otherAnswers.value[card.id]?.trim() : ''
    if (labels.length || other) answers[card.id] = [...labels, other ? `其他：${other}` : ''].filter(Boolean).join('；')
  }
  emit('submit', { answers, conservativeGapIds })
}

function activeOptionsFor(card: PreparationClarification['cards'][number]): ClarificationOption[] {
  const options = card.options?.length ? card.options : fallbackOptions
  return card.allowConservativeScope
    ? [...options, { id: conservativeOptionId, label: '按保守范围生成' }]
    : options
}
</script>

<style scoped>
.clarification-card { width: min(100%, 780px); overflow: hidden; background: #fff; border: 1px solid #d9d9d9; border-radius: 12px; box-shadow: 0 1px 3px rgba(15, 23, 42, .08); }
.clarification-card--collapsed { padding: 9px 12px; }
.clarification-card__restore { width: 100%; color: #31343a; text-align: left; cursor: pointer; background: transparent; border: 0; font: inherit; font-size: 13px; font-weight: 600; }
.clarification-card__header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 12px 10px; }
.clarification-card__heading { display: flex; flex: 1; align-items: center; min-width: 0; gap: 9px; }
.clarification-card__progress { flex: 0 0 auto; padding: 3px 7px; color: #1f7a45; background: #dcfce7; border-radius: 999px; font-size: 12px; font-weight: 700; line-height: 1.15; }
.clarification-card h3 { min-width: 0; margin: 0; color: #191919; overflow-wrap: anywhere; font-size: 14px; font-weight: 700; line-height: 1.45; }
.clarification-card h3 small { color: #424242; font-size: 12px; font-weight: 600; }
.clarification-card__header-actions { display: grid; grid-template-columns: repeat(2, 24px); flex: 0 0 auto; gap: 4px; align-self: flex-start; align-items: center; white-space: nowrap; }
.clarification-card__header-actions button { display: grid; width: 24px; height: 24px; padding: 0; color: #424242; cursor: pointer; place-items: center; background: transparent; border: 0; border-radius: 4px; }
.clarification-card__header-actions button:hover, .clarification-card__header-actions button:focus-visible { background: #f0f1f2; outline: none; }
.clarification-card__header-actions :deep(.n-icon) { display: block; font-size: 15px; line-height: 1; }
.clarification-card__options { display: grid; gap: 4px; padding: 0 12px; }
.clarification-option { display: flex; align-items: center; justify-content: space-between; width: 100%; min-height: 49px; padding: 8px 10px; color: #202124; text-align: left; cursor: pointer; background: #f4f4f4; border: 1px solid transparent; border-radius: 5px; font: inherit; }
.clarification-option:hover:not(:disabled), .clarification-option--selected { background: #f0f1f2; border-color: #d3d6db; }
.clarification-option:focus-visible, .clarification-option__other-trigger:focus-visible, .clarification-option__other-input:focus-visible { outline: 2px solid #8ca7cc; outline-offset: 1px; }
.clarification-option:disabled { cursor: not-allowed; opacity: .65; }
.clarification-option__copy { display: grid; min-width: 0; gap: 2px; }
.clarification-option__copy strong { font-size: 13px; font-weight: 500; line-height: 1.25; }
.clarification-option__copy small { overflow: hidden; color: #8a8a8a; font-size: 12px; line-height: 1.25; text-overflow: ellipsis; white-space: nowrap; }
.clarification-option__indicator { display: inline-flex; flex: 0 0 auto; align-items: center; justify-content: center; width: 16px; height: 16px; margin-left: 12px; color: #4f5967; background: #fafafa; border: 1px solid #c9cdd3; border-radius: 4px; font-size: 11px; font-weight: 700; }
.clarification-option--selected .clarification-option__indicator { color: #fff; background: #5b6471; border-color: #5b6471; }
.clarification-option--other { display: block; min-height: 0; padding: 0; }
.clarification-option__other-trigger { display: flex; align-items: center; justify-content: space-between; width: 100%; min-height: 40px; padding: 8px 10px; color: inherit; text-align: left; cursor: pointer; background: transparent; border: 0; font: inherit; }
.clarification-option__other-input { box-sizing: border-box; width: calc(100% - 18px); height: 31px; padding: 6px 8px; margin: 0 9px 9px; color: #30343a; background: #fff; border: 1px solid #d9d9d9; border-radius: 5px; font: inherit; font-size: 12px; }
.clarification-card__error { padding: 0 12px; margin: 8px 0 0; color: #b42318; font-size: 12px; }
.clarification-card__footer { display: flex; align-items: center; gap: 7px; padding: 12px; }
.clarification-card__footer-spacer { flex: 1; }
.clarification-card__button { min-height: 25px; padding: 2px 9px; font: inherit; font-size: 12px; line-height: 1.2; cursor: pointer; border-radius: 5px; }
.clarification-card__button--secondary { color: #30343a; background: #fff; border: 1px solid #dfe1e5; }
.clarification-card__button--primary { color: #fff; background: #0d0d0d; border: 1px solid #0d0d0d; font-weight: 600; }
.clarification-card__button--primary:hover:not(:disabled) { background: #262626; border-color: #262626; }
.clarification-card__button:disabled { cursor: not-allowed; opacity: .55; }
@media (max-width: 640px) { .clarification-card__header { align-items: flex-start; } .clarification-option__copy small { white-space: normal; } }
</style>
