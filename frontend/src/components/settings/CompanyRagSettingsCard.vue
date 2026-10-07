<template>
  <section class="settings-card ta-card">
    <header class="settings-card__header">
      <span class="settings-card__icon">
        <n-icon :component="BookOutline" />
      </span>
      <div class="settings-card__title">
        <h2>公司知识库配置</h2>
        <p>仅在生成测试方案时查询公司的规范、流程和测试经验；项目资料仍由项目上下文检索。</p>
      </div>
      <label class="settings-toggle" :class="{ 'settings-toggle--checked': form.enabled }">
        <input v-model="form.enabled" type="checkbox" />
        <span class="settings-toggle__track"><span class="settings-toggle__thumb" /></span>
        <span class="settings-toggle__label">启用</span>
      </label>
    </header>

    <form class="settings-form" @submit.prevent>
      <label class="settings-field">
        <span class="settings-field__label">知识库 API URL</span>
        <span class="settings-control">
          <n-icon class="settings-control__icon" :component="LinkOutline" />
          <input v-model="form.apiBaseUrl" type="url" placeholder="https://rag.example.com/api" autocomplete="url" />
        </span>
      </label>

      <label class="settings-field">
        <span class="settings-field__label">API KEY</span>
        <span class="settings-control">
          <n-icon class="settings-control__icon" :component="KeyOutline" />
          <input v-model="form.apiKey" class="settings-control__code" type="password" placeholder="首次保存时填写；已保存的密钥不会回显" autocomplete="new-password" />
        </span>
      </label>

      <div class="company-rag-grid">
        <label class="settings-field">
          <span class="settings-field__label">超时时间（秒）</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="TimerOutline" />
            <input v-model.number="form.timeoutSeconds" type="number" min="3" max="120" />
          </span>
        </label>
        <label class="settings-field">
          <span class="settings-field__label">检索条数</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="ListOutline" />
            <input v-model.number="form.topK" type="number" min="1" max="20" />
          </span>
        </label>
      </div>

      <p class="settings-hint">协议：POST <code>/chunk/retrieve</code>，请求字段为 <code>query</code>、<code>topK</code>、<code>similarityThreshold</code>、<code>retrieveStrategy</code>。</p>

      <footer class="settings-actions">
        <button class="settings-button settings-button--secondary" type="button" :disabled="!canTest || testing" @click="testConnection">
          {{ testing ? '测试中…' : '测试连接' }}
        </button>
        <button class="settings-button settings-button--primary" type="button" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存设置' }}
        </button>
      </footer>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useMessage, NIcon } from 'naive-ui'
import { BookOutline, KeyOutline, LinkOutline, ListOutline, TimerOutline } from '@vicons/ionicons5'
import {
  getCompanyRagConfig,
  testCompanyRagConnection,
  updateCompanyRagConfig,
  type CompanyRagConfig,
  type UpdateCompanyRagConfig
} from '@/api/companyRagApi'

const message = useMessage()
const saving = ref(false)
const testing = ref(false)
const configured = ref(false)
const form = reactive({
  enabled: false,
  apiBaseUrl: '',
  apiKey: '',
  timeoutSeconds: 30,
  topK: 5,
  similarityThreshold: 0.35,
  retrieveStrategy: 3
})

const canTest = computed(() => configured.value && Boolean(form.apiBaseUrl.trim()))

function sync(config: CompanyRagConfig) {
  form.enabled = config.enabled
  form.apiBaseUrl = config.api_base_url || ''
  form.apiKey = ''
  form.timeoutSeconds = config.timeout_seconds || 30
  form.topK = config.top_k || 5
  form.similarityThreshold = config.similarity_threshold ?? 0.35
  form.retrieveStrategy = config.retrieve_strategy || 3
  configured.value = config.api_key_set
}

async function load() {
  try {
    sync(await getCompanyRagConfig())
  } catch (error) {
    message.error(`读取公司知识库配置失败：${error instanceof Error ? error.message : '请稍后重试'}`)
  }
}

function payload(): UpdateCompanyRagConfig {
  const next: UpdateCompanyRagConfig = {
    enabled: form.enabled,
    api_base_url: form.apiBaseUrl.trim(),
    timeout_seconds: Number(form.timeoutSeconds) || 30,
    top_k: Number(form.topK) || 5,
    similarity_threshold: form.similarityThreshold,
    retrieve_strategy: form.retrieveStrategy
  }
  if (form.apiKey.trim()) next.api_key = form.apiKey.trim()
  return next
}

async function save() {
  if (saving.value) return
  if (!form.apiBaseUrl.trim()) {
    message.error('请填写公司知识库 API URL。')
    return
  }
  if (!configured.value && !form.apiKey.trim()) {
    message.error('首次保存公司知识库连接时，请填写 API KEY。')
    return
  }
  saving.value = true
  try {
    sync(await updateCompanyRagConfig(payload()))
    message.success('公司知识库配置已保存。')
  } catch (error) {
    message.error(`保存失败：${error instanceof Error ? error.message : '请稍后重试'}`)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  if (!canTest.value || testing.value) return
  testing.value = true
  try {
    const result = await testCompanyRagConnection()
    if (result.success) message.success(`公司知识库连接成功（${result.latency_ms} ms）。`)
    else message.error(`公司知识库连接失败：${result.message}`)
  } catch (error) {
    message.error(`公司知识库连接失败：${error instanceof Error ? error.message : '请稍后重试'}`)
  } finally {
    testing.value = false
  }
}

onMounted(() => { void load() })
</script>

<style scoped>
.settings-card { padding: 24px; border: 0; border-radius: 12px; }
.settings-card__header { display: flex; gap: 12px; align-items: center; padding-bottom: 16px; margin-bottom: 24px; border-bottom: 1px solid var(--ta-border); }
.settings-card__icon { display: inline-flex; flex: 0 0 auto; align-items: center; justify-content: center; width: 36px; height: 36px; color: var(--ta-primary); background: var(--ta-surface-low); border-radius: var(--ta-radius-md); }
.settings-card__title { min-width: 0; }.settings-card h2, .settings-card p { margin: 0; }.settings-card h2 { color: var(--ta-text-strong); font-size: var(--text-card-title); font-weight: var(--font-semibold); line-height: 24px; }.settings-card p { color: #666; font-size: var(--text-ui); line-height: 21px; }
.settings-form { display: flex; flex-direction: column; gap: 20px; }.settings-field { display: flex; flex-direction: column; gap: 4px; }.settings-field__label { color: #444; font-size: var(--text-meta); font-weight: var(--font-medium); line-height: var(--leading-meta); }
.settings-control { position: relative; display: flex; align-items: center; min-height: 38px; padding: 8px 12px; color: var(--ta-text-strong); background: var(--ta-surface); border: 1px solid var(--ta-border); border-radius: var(--ta-radius-md); }.settings-control:focus-within { border-color: var(--ta-primary); box-shadow: 0 0 0 3px rgba(0,0,0,.08); }.settings-control__icon { flex: 0 0 auto; margin-right: 8px; color: var(--ta-outline); font-size: 20px; }.settings-control input { width: 100%; min-width: 0; padding: 0; margin: 0; color: var(--ta-text-strong); font: inherit; font-size: var(--text-md); line-height: var(--leading-ui); background: transparent; border: 0; outline: 0; }.settings-control__code { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.company-rag-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }.settings-actions { display: flex; gap: 12px; justify-content: flex-end; padding-top: 16px; }.settings-button { min-height: 36px; padding: 0 16px; font-size: var(--text-ui); font-weight: var(--font-medium); cursor: pointer; border-radius: var(--ta-radius-md); }.settings-button:disabled { cursor: default; opacity: .72; }.settings-button--secondary { color: var(--ta-primary); background: var(--ta-surface); border: 1px solid var(--ta-border); }.settings-button--primary { color: var(--ta-surface); background: var(--ta-primary); border: 1px solid var(--ta-primary); }.settings-hint { color: var(--ta-outline) !important; font-size: var(--text-meta) !important; }.settings-hint code { padding: 1px 4px; background: var(--ta-surface-low); border-radius: 4px; }
.settings-toggle { display: inline-flex; flex: 0 0 auto; gap: 12px; align-items: center; margin-left: auto; cursor: pointer; }.settings-toggle input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }.settings-toggle__track { position: relative; width: 44px; height: 24px; background: var(--ta-surface-variant); border-radius: 999px; transition: background .18s ease; }.settings-toggle__thumb { position: absolute; top: 2px; left: 2px; width: 20px; height: 20px; background: var(--ta-surface); border: 1px solid var(--ta-border); border-radius: 50%; transition: transform .18s ease; }.settings-toggle--checked .settings-toggle__track { background: var(--ta-primary); }.settings-toggle--checked .settings-toggle__thumb { transform: translateX(20px); }.settings-toggle__label { color: var(--ta-outline); font-size: var(--text-meta); font-weight: var(--font-medium); }
@media (max-width: 640px) { .settings-card { padding: 18px; }.settings-card__header { align-items: flex-start; flex-wrap: wrap; }.settings-toggle { width: 100%; margin-left: 48px; }.company-rag-grid { grid-template-columns: 1fr; }.settings-actions { flex-wrap: wrap; }.settings-button { flex: 1 1 120px; } }
</style>
