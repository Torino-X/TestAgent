<template>
  <n-dropdown
    :options="dropdownOptions"
    :menu-props="menuProps"
    trigger="click"
    :placement="placement"
    :show-arrow="false"
    @select="selectOption"
    @update:show="updateMenuState"
  >
    <button
      :id="id"
      ref="trigger"
      class="app-select"
      :class="{
        'app-select--block': block,
        'app-select--compact': compact,
        'app-select--open': menuOpen,
        'app-select--placeholder': !selectedOption
      }"
      :style="triggerStyle"
      type="button"
      :disabled="disabled"
      :aria-label="ariaLabel"
      aria-haspopup="menu"
      :aria-expanded="menuOpen"
    >
      <span class="app-select__label">{{ selectedOption?.label ?? placeholder }}</span>
      <n-icon class="app-select__chevron" :component="ChevronDownOutline" :size="15" />
    </button>
  </n-dropdown>
</template>

<script setup lang="ts">
import { computed, h, nextTick, ref } from 'vue'
import { NDropdown, NIcon, type DropdownOption } from 'naive-ui'
import { CheckmarkOutline, ChevronDownOutline } from '@vicons/ionicons5'
import type { AppSelectOption, AppSelectValue } from './appSelect'

const props = withDefaults(defineProps<{
  modelValue: AppSelectValue
  options: readonly AppSelectOption[]
  id?: string
  ariaLabel?: string
  placeholder?: string
  disabled?: boolean
  block?: boolean
  compact?: boolean
  minWidth?: number | string
  menuWidth?: number | string
  placement?: 'bottom-start' | 'bottom-end' | 'top-start' | 'top-end'
}>(), {
  id: undefined,
  ariaLabel: '请选择',
  placeholder: '请选择',
  disabled: false,
  block: false,
  compact: false,
  minWidth: 0,
  menuWidth: undefined,
  placement: 'bottom-start'
})

const emit = defineEmits<{
  'update:modelValue': [value: AppSelectValue]
  change: [value: AppSelectValue]
}>()

const trigger = ref<HTMLButtonElement | null>(null)
const menuOpen = ref(false)
const measuredWidth = ref(160)
const selectedOption = computed(() => props.options.find((option) => Object.is(option.value, props.modelValue)))
const triggerStyle = computed(() => ({
  minWidth: toCssSize(props.minWidth)
}))
const optionLabelStyle = {
  overflow: 'visible',
  textOverflow: 'clip',
  whiteSpace: 'nowrap'
}
const dropdownOptions = computed<DropdownOption[]>(() => props.options.map((option) => {
  const selected = Object.is(option.value, props.modelValue)
  return {
    key: option.value,
    disabled: option.disabled,
    props: {
      class: [
        'app-select-menu__option',
        selected ? 'app-select-menu__option--selected' : '',
        option.description ? 'app-select-menu__option--described' : ''
      ].filter(Boolean).join(' '),
      'aria-selected': selected
    },
    label: () => h('span', { class: 'app-select-option' }, [
      h('span', { class: 'app-select-option__copy' }, [
        h('span', { class: 'app-select-option__label', style: optionLabelStyle }, option.label),
        option.description
          ? h('small', { class: 'app-select-option__description' }, option.description)
          : null
      ]),
      selected
        ? h(NIcon, { class: 'app-select-option__check', component: CheckmarkOutline, size: 17 })
        : h('span', { class: 'app-select-option__check-space', 'aria-hidden': 'true' })
    ])
  }
}))
const menuProps = () => ({
  class: 'app-select-menu',
  style: `width: max-content; min-width: ${resolvedMenuWidth()}; max-width: none;`
})

function toCssSize(value: number | string | undefined) {
  if (value === undefined || value === '' || value === 0) return undefined
  return typeof value === 'number' ? `${value}px` : value
}

function resolvedMenuWidth() {
  const explicitWidth = toCssSize(props.menuWidth)
  return explicitWidth ?? `${measuredWidth.value}px`
}

function selectOption(key: string | number) {
  const option = props.options.find((item) => Object.is(item.value, key))
  if (!option || option.disabled || props.disabled) return
  emit('update:modelValue', option.value)
  emit('change', option.value)
}

function updateMenuState(show: boolean) {
  menuOpen.value = show
  if (!show) return
  void nextTick(() => {
    const width = trigger.value?.getBoundingClientRect().width ?? 0
    measuredWidth.value = Math.max(120, Math.round(width))
  })
}
</script>

<style scoped>
.app-select {
  display: inline-flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  min-width: 120px;
  height: 38px;
  padding: 0 10px 0 12px;
  color: #242424;
  font: inherit;
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  background: #ffffff;
  border: 1px solid #dedede;
  border-radius: 9px;
  outline: none;
  transition: background-color 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
}

.app-select:hover:not(:disabled),
.app-select--open {
  background: #fafafa;
  border-color: #c9c9c7;
}

.app-select:focus-visible {
  border-color: #707070;
  box-shadow: 0 0 0 3px rgba(23, 23, 23, 0.1);
}

.app-select:disabled {
  color: #999999;
  cursor: not-allowed;
  background: #f5f5f3;
  opacity: 0.72;
}

.app-select--block {
  width: 100%;
}

.app-select--compact {
  height: 30px;
  padding-right: 8px;
  padding-left: 10px;
  font-size: 12px;
  border-radius: 7px;
}

.app-select--placeholder {
  color: #777777;
}

.app-select__label {
  min-width: 0;
  overflow: hidden;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-select__chevron {
  flex: 0 0 auto;
  color: #505050;
  transition: transform 160ms ease;
}

.app-select--open .app-select__chevron {
  transform: rotate(180deg);
}

@media (prefers-reduced-motion: reduce) {
  .app-select,
  .app-select__chevron {
    transition: none;
  }
}
</style>
