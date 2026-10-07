<template>
  <AppLayout>
    <div class="settings-view">
      <div class="settings-view__inner">
        <header class="settings-view__header">
          <h1>系统设置</h1>
          <p>配置 TestAgent 的核心运行参数与依赖服务。</p>
        </header>
        <SettingsForm />
      </div>
    </div>
  </AppLayout>
</template>

<script setup lang="ts">
import AppLayout from '@/components/layout/AppLayout.vue'
import SettingsForm from '@/components/settings/SettingsForm.vue'
</script>

<style scoped>
.settings-view {
  flex: 1;
  height: 100%;
  overflow-y: auto;
  padding: 40px;
  background: var(--ta-bg-main);
}

.settings-view__inner {
  width: min(100%, 800px);
  margin: 0 auto;
  padding-bottom: 40px;
}

.settings-view__header {
  margin-bottom: 24px;
}

.settings-view__header h1 {
  margin: 0 0 4px;
  color: var(--ta-text-strong);
  font-size: var(--text-page-title);
  font-weight: var(--font-semibold);
  letter-spacing: -0.02em;
  line-height: var(--leading-title);
}

.settings-view__header p {
  margin: 0;
  color: #666666;
  font-size: var(--text-md);
  font-weight: var(--font-regular);
  line-height: 23px;
}

@media (max-width: 860px) {
  .settings-view {
    padding: 24px 16px 32px;
  }

  .settings-view__header h1 {
    font-size: 24px;
    line-height: 32px;
  }

  .settings-view__header p {
    font-size: var(--text-md);
    line-height: 23px;
  }
}
</style>
