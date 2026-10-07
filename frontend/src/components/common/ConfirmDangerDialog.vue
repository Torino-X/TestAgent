<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="delete-confirm"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="titleId"
      @click.self="onCancel"
    >
      <div class="delete-confirm__dialog">
        <h2 :id="titleId" class="delete-confirm__title">{{ title }}</h2>
        <p class="delete-confirm__body">
          <slot name="body">
            {{ body }}
          </slot>
        </p>
        <p v-if="hint" class="delete-confirm__hint">{{ hint }}</p>
        <div class="delete-confirm__actions">
          <button
            class="delete-confirm__button delete-confirm__button--secondary"
            type="button"
            @click="onCancel"
          >
            {{ cancelLabel }}
          </button>
          <button
            class="delete-confirm__button delete-confirm__button--danger"
            type="button"
            @click="onConfirm"
          >
            {{ confirmLabel }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed } from 'vue'

withDefaults(
  defineProps<{
    visible: boolean
    title: string
    body?: string
    hint?: string
    confirmLabel?: string
    cancelLabel?: string
  }>(),
  {
    body: '',
    hint: '',
    confirmLabel: '删除',
    cancelLabel: '取消'
  }
)

const emit = defineEmits<{
  (event: 'cancel'): void
  (event: 'confirm'): void
}>()

const titleId = computed(() => `delete-confirm-title-${Math.random().toString(36).slice(2, 9)}`)

function onCancel() {
  emit('cancel')
}

function onConfirm() {
  emit('confirm')
}
</script>

<style scoped>
.delete-confirm {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  padding: 24px;
  background: rgba(248, 249, 255, 0.72);
  backdrop-filter: blur(2px);
  place-items: center;
}

.delete-confirm__dialog {
  width: min(480px, 100%);
  padding: 22px 22px 18px;
  color: var(--ta-text);
  background: #ffffff;
  border: 1px solid rgba(194, 198, 216, 0.9);
  border-radius: 14px;
  box-shadow:
    0 18px 48px rgba(15, 23, 42, 0.14),
    0 2px 8px rgba(15, 23, 42, 0.08);
}

.delete-confirm__title {
  margin: 0 0 18px;
  color: var(--ta-text-strong);
  font-size: 20px;
  font-weight: 650;
  line-height: 28px;
}

.delete-confirm__body {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: 15px;
  line-height: 24px;
}

.delete-confirm__body :deep(strong) {
  font-weight: 600;
}

.delete-confirm__hint {
  margin: 8px 0 28px;
  color: var(--ta-text-muted);
  font-size: 14px;
  line-height: 22px;
}

.delete-confirm__actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}

.delete-confirm__button {
  min-width: 64px;
  height: 40px;
  padding: 0 18px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  border-radius: 999px;
  transition:
    background 160ms ease,
    border-color 160ms ease,
    box-shadow 160ms ease;
}

.delete-confirm__button--secondary {
  color: var(--ta-text);
  background: #ffffff;
  border: 1px solid var(--ta-border);
}

.delete-confirm__button--secondary:hover {
  background: var(--ta-surface-low);
}

.delete-confirm__button--danger {
  color: #ffffff;
  background: #f20d32;
  border: 1px solid #f20d32;
}

.delete-confirm__button--danger:hover {
  background: #d90b2d;
  border-color: #d90b2d;
}
</style>
