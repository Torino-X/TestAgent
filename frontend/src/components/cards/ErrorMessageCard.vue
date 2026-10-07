<template>
  <section class="error-card">
    <span>{{ message }}</span>
    <button v-if="retryable" class="error-card__retry" type="button" @click="$emit('retry')">重试</button>
  </section>
</template>

<script setup lang="ts">
defineProps<{
  message: string
  retryable?: boolean
}>()

defineEmits<{
  retry: []
}>()
</script>

<style scoped>
.error-card {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  width: min(100%, var(--ta-content-width));
  padding: 14px 16px;
  color: var(--ta-error);
  background: var(--ta-error-soft);
  border: 1px solid rgba(186, 26, 26, 0.22);
  border-radius: var(--ta-radius-lg);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
}

.error-card__retry {
  flex: 0 0 auto;
  padding: 5px 10px;
  color: var(--ta-error);
  font-size: var(--text-ui);
  font-weight: var(--font-medium);
  line-height: var(--leading-ui);
  cursor: pointer;
  background: #ffffff;
  border: 1px solid rgba(186, 26, 26, 0.24);
  border-radius: var(--ta-radius-sm);
}
</style>
