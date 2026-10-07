<template>
  <AppSelect
    :id="id"
    :model-value="modelValue"
    :options="options"
    :aria-label="ariaLabel"
    :block="block"
    :min-width="142"
    :menu-width="block ? undefined : 300"
    @update:model-value="updateValue"
  />
</template>

<script setup lang="ts">
import AppSelect from '@/components/common/AppSelect.vue'
import type { AppSelectOption } from '@/components/common/appSelect'

withDefaults(defineProps<{
  modelValue: string
  options: AppSelectOption<string>[]
  id?: string
  ariaLabel?: string
  block?: boolean
}>(), {
  id: undefined,
  ariaLabel: '请选择',
  block: false
})

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

function updateValue(value: string | number) {
  emit('update:modelValue', String(value))
}
</script>
