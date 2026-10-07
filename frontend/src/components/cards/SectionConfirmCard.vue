<template>
  <section class="section-card" :class="{ 'section-card--confirmed': confirmed }" aria-labelledby="section-confirm-title">
    <header class="section-card__header">
      <div class="section-card__heading">
        <div class="section-card__title-line">
          <h3 id="section-confirm-title">确认章节处理策略</h3>
          <div class="section-card__stats" aria-label="章节处理统计">
            <span
              v-for="stat in sectionStats"
              :key="stat.action"
              class="section-card__stat"
              :class="`section-card__stat--${stat.action}`"
            >
              {{ stat.label }} {{ stat.count }}
            </span>
          </div>
        </div>
        <p>
          已从模板中识别 {{ localSections.length }} 个章节。
          <span v-if="userConstraintCount">其中 {{ userConstraintCount }} 个按你的提示词指定。</span>
          请确认哪些章节由 AI 生成，哪些保留模板原文。
        </p>
      </div>
      <div class="section-card__batch-actions">
        <button
          type="button"
          class="section-card__batch-button"
          :class="{ 'section-card__batch-button--active': activeBatchAction === 'ai_generate' }"
          :disabled="confirmed || submitting"
          @click="batchUpdateAction('ai_generate')"
        >全部 AI 生成</button>
        <button type="button" class="section-card__batch-button" :disabled="confirmed || submitting" @click="restoreSuggestedActions">恢复建议</button>
      </div>
    </header>

    <div class="section-card__table-wrap">
      <table class="section-strategy-table">
        <thead>
          <tr>
            <th>章节名称</th>
            <th>Agent 建议</th>
            <th>处理动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(section, index) in localSections" :key="`${section.id}_${index}`">
            <td>
              <div class="section-card__name-cell">
                <strong>{{ displaySectionName(section) }}</strong>
                <span v-if="section.constraintSource === 'user_prompt'" class="section-card__user-chip">用户指定</span>
              </div>
            </td>
            <td><span class="section-card__suggestion">{{ actionLabel(section.suggestedAction) }}</span></td>
            <td>
              <AppSelect
                :model-value="section.action"
                :options="actionOptions"
                :disabled="confirmed || submitting"
                compact
                block
                aria-label="章节处理动作"
                @update:model-value="updateAction(index, $event as SectionAction)"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="section-card__footer">
      <button type="button" :disabled="confirmed || submitting" @click="submit">
        {{ confirmed ? '已确认章节策略' : submitting ? '正在确认…' : '确认章节策略' }}
      </button>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppSelect from '@/components/common/AppSelect.vue'
import type { AppSelectOption } from '@/components/common/appSelect'
import { sectionActionOptions, type SectionAction, type SectionItem } from '@/types'

const props = withDefaults(
  defineProps<{
    sections: SectionItem[]
    confirmed?: boolean
    submitting?: boolean
  }>(),
  { confirmed: false, submitting: false }
)

const emit = defineEmits<{ confirm: [sections: SectionItem[]] }>()
const localSections = ref<SectionItem[]>([])
const activeBatchAction = ref<SectionAction | null>(null)
const actionOptions: AppSelectOption<SectionAction>[] = sectionActionOptions
  .filter((option) => option.value === 'ai_generate' || option.value === 'keep_template')
  .map((option) => ({
  label: option.label,
  value: option.value
}))
const sectionStats = computed(() => {
  const counts = new Map<SectionAction, number>()
  for (const section of localSections.value) counts.set(section.action, (counts.get(section.action) ?? 0) + 1)
  return sectionActionOptions
    .map((option) => ({ action: option.value, label: option.label, count: counts.get(option.value) ?? 0 }))
    .filter((stat) => stat.count > 0)
})
const userConstraintCount = computed(() => localSections.value.filter((section) => section.constraintSource === 'user_prompt').length)

watch(
  () => props.sections,
  (sections) => {
    localSections.value = sections.map((section) => ({
      ...section,
      action: toSelectableAction(section.action)
    }))
    activeBatchAction.value = null
  },
  { immediate: true }
)

function updateAction(index: number, action: SectionAction) {
  const target = localSections.value[index]
  if (target) {
    target.action = action
    activeBatchAction.value = null
  }
}

function batchUpdateAction(action: SectionAction) {
  localSections.value = localSections.value.map((section) => ({ ...section, action }))
  activeBatchAction.value = action
}

function restoreSuggestedActions() {
  localSections.value = localSections.value.map((section) => ({
    ...section,
    action: toSelectableAction(section.suggestedAction)
  }))
  activeBatchAction.value = null
}

function toSelectableAction(action: SectionAction): SectionAction {
  return action === 'ai_generate' ? action : 'keep_template'
}

function actionLabel(action: SectionAction) {
  return sectionActionOptions.find((option) => option.value === action)?.label ?? action
}

function displaySectionName(section: SectionItem) {
  return section.title.trim() || section.code.trim()
}

function submit() {
  emit('confirm', localSections.value)
}
</script>

<style scoped>
.section-card { width: min(100%, 780px); overflow: hidden; background: #fff; border: 1px solid #d9d9d9; border-radius: 12px; box-shadow: 0 1px 3px rgba(15, 23, 42, .08); }
.section-card__header { display: grid; gap: 12px; padding: 13px 12px 10px; border-bottom: 1px solid #e7e8eb; }
.section-card__heading { display: grid; gap: 7px; min-width: 0; }
.section-card__title-line { display: flex; gap: 12px; align-items: center; justify-content: space-between; }
.section-card h3 { margin: 0; color: #191919; font-size: 14px; font-weight: 700; line-height: 1.45; }
.section-card__heading p { margin: 0; color: #5f6368; font-size: 13px; line-height: 1.55; }
.section-card__stats { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }
.section-card__stat { padding: 2px 8px; font-size: 11px; font-weight: 600; line-height: 16px; border-radius: 999px; }
.section-card__stat--ai_generate { color: #17305c; background: #d3e4fe; }
.section-card__stat--keep_template, .section-card__stat--skip { color: #5f6368; background: #eef0f4; }
.section-card__stat--manual_fill { color: #1b3559; background: #d5e4f8; }
.section-card__batch-actions { display: flex; gap: 8px; align-items: center; justify-content: space-between; }
.section-card__batch-actions button, .section-card__footer button { min-height: 28px; padding: 3px 10px; font: inherit; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 5px; }
.section-card__batch-actions button { color: #475569; background: #fff; border: 1px solid #cbd5e1; }
.section-card__batch-actions button.section-card__batch-button--active { color: #fff; background: #2563eb; border-color: #2563eb; }
.section-card__batch-actions button:hover:not(:disabled) { background: #f3f4f6; }
.section-card__batch-actions button.section-card__batch-button--active:hover:not(:disabled) { background: #1d4ed8; border-color: #1d4ed8; }
.section-card__batch-actions button:disabled, .section-card__footer button:disabled { cursor: not-allowed; opacity: .55; }
.section-card__table-wrap { max-height: 260px; overflow: auto; }
.section-strategy-table { width: 100%; min-width: 600px; border-collapse: collapse; }
.section-strategy-table th { position: sticky; top: 0; z-index: 1; height: 36px; padding: 0 12px; color: #6b7280; font-size: 12px; font-weight: 500; text-align: left; background: #f2f4f6; border-bottom: 1px solid #dfe1e5; }
.section-strategy-table th:nth-child(1) { width: 40%; }.section-strategy-table th:nth-child(2) { width: 25%; }.section-strategy-table th:nth-child(3) { width: 35%; }
.section-strategy-table td { height: 44px; padding: 8px 12px; color: #4b5563; font-size: 13px; border-bottom: 1px solid #eef0f4; }
.section-card__name-cell { display: flex; gap: 7px; align-items: center; }.section-card__name-cell strong { color: #191919; font-weight: 600; }
.section-card__user-chip { padding: 2px 6px; color: #365314; background: #ecfccb; border-radius: 4px; font-size: 10px; font-weight: 600; white-space: nowrap; }
.section-card__suggestion { display: inline-flex; padding: 4px 9px; color: #222; background: #f3f4f6; border-radius: 999px; font-weight: 600; }
.section-card__footer { display: flex; justify-content: flex-end; padding: 12px; background: #f7f8fa; }
.section-card__footer button { color: #fff; background: #0d0d0d; border: 1px solid #0d0d0d; }.section-card__footer button:hover:not(:disabled) { background: #262626; border-color: #262626; }
.section-card--confirmed { border-color: rgba(31, 122, 69, .4); }
@media (max-width: 640px) { .section-card__title-line { align-items: flex-start; flex-direction: column; }.section-card__stats { justify-content: flex-start; }.section-card__batch-actions { flex-wrap: wrap; } }
</style>
