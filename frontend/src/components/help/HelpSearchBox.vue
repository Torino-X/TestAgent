<template>
  <label class="help-search" :class="{ 'help-search--large': large }">
    <n-icon class="help-search__icon" :component="SearchOutline" :size="20" aria-hidden="true" />
    <input
      :value="modelValue"
      type="search"
      :placeholder="placeholder"
      aria-label="搜索帮助文档"
      @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      @keydown.enter="$emit('submit')"
    />
    <span v-if="loading" class="help-search__spinner" aria-label="正在搜索"></span>
    <button v-else-if="modelValue" type="button" aria-label="清空搜索" @click="$emit('update:modelValue', '')">
      <n-icon :component="CloseCircle" :size="18" />
    </button>
  </label>
</template>

<script setup lang="ts">
import { NIcon } from 'naive-ui'
import { CloseCircle, SearchOutline } from '@vicons/ionicons5'

withDefaults(defineProps<{
  modelValue: string
  placeholder?: string
  loading?: boolean
  large?: boolean
}>(), {
  placeholder: '搜索帮助文档',
  loading: false,
  large: false
})

defineEmits<{
  'update:modelValue': [value: string]
  submit: []
}>()
</script>

<style scoped>
.help-search {
  display: flex;
  align-items: center;
  width: min(480px, 100%);
  height: 42px;
  padding: 0 13px;
  gap: 10px;
  background: #fff;
  border: 1px solid #d9dce1;
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(17, 24, 39, 0.02);
  transition: border-color 160ms ease, box-shadow 160ms ease;
}

.help-search:focus-within {
  border-color: #8b8f97;
  box-shadow: 0 0 0 3px rgba(17, 24, 39, 0.06);
}

.help-search--large {
  width: min(680px, 100%);
  height: 54px;
  padding: 0 16px;
  border-radius: 14px;
  box-shadow: 0 8px 28px rgba(17, 24, 39, 0.07);
}

.help-search__icon { color: #6b7280; }

.help-search input {
  min-width: 0;
  flex: 1;
  color: #111827;
  font-size: 15px;
  background: transparent;
  border: 0;
  outline: 0;
}

.help-search input::placeholder { color: #9ca3af; }
.help-search input::-webkit-search-cancel-button { display: none; }

.help-search button {
  display: grid;
  padding: 2px;
  color: #9ca3af;
  cursor: pointer;
  background: transparent;
  border: 0;
  place-items: center;
}

.help-search__spinner {
  width: 16px;
  height: 16px;
  border: 2px solid #e5e7eb;
  border-top-color: #111827;
  border-radius: 50%;
  animation: help-spin 700ms linear infinite;
}

@keyframes help-spin { to { transform: rotate(360deg); } }
</style>
