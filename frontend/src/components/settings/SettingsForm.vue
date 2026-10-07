<template>
  <div class="settings-stack">
    <section class="settings-card ta-card">
      <header class="settings-card__header">
        <span class="settings-card__icon">
          <n-icon :component="HardwareChipOutline" />
        </span>
        <div class="settings-card__title">
          <h2>模型配置</h2>
          <p>设置底层语言模型 API 信息。</p>
        </div>
      </header>

      <form class="settings-form" @submit.prevent>
        <label class="settings-field">
          <span class="settings-field__label">API URL</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="LinkOutline" />
            <input v-model="form.apiBaseUrl" type="url" placeholder="https://api.openai.com/v1" />
          </span>
        </label>

        <label class="settings-field">
          <span class="settings-field__label">API KEY</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="KeyOutline" />
            <input v-model="form.apiKey" class="settings-control__code" type="password" placeholder="sk-..." />
          </span>
        </label>

        <label class="settings-field">
          <span class="settings-field__label">模型名称</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="HardwareChipOutline" />
            <input v-model="form.modelName" type="text" placeholder="请输入模型名称" autocomplete="off" />
          </span>
        </label>

        <label class="settings-field">
          <span class="settings-field__label">上下文窗口大小（单位: K）</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="HardwareChipOutline" />
            <input
              v-model.number="form.contextWindowK"
              type="number"
              min="1"
              step="1"
              placeholder="请按模型真实上下文窗口大小填写"
              autocomplete="off"
            />
          </span>
        </label>

        <footer class="settings-actions">
          <button
            class="settings-button settings-button--secondary"
            type="button"
            :disabled="!canTestModelConnection || testingModel"
            @click="testModel"
          >
            {{ testingModel ? '测试中' : '测试连接' }}
          </button>
          <button class="settings-button settings-button--primary" type="button" @click="save">保存设置</button>
        </footer>
      </form>
    </section>

    <CompanyRagSettingsCard />

    <section class="settings-card ta-card">
      <header class="settings-card__header">
        <span class="settings-card__icon">
          <n-icon :component="ImageOutline" />
        </span>
        <div class="settings-card__title">
          <h2>图片理解配置</h2>
          <p>启用后，需求文档里的流程图 / 原型图会被 OCR + 视觉模型抽取并送入 LLM。</p>
        </div>
        <label
          class="settings-toggle"
          :class="{ 'settings-toggle--checked': imageForm.enableInDocParsing, 'settings-toggle--disabled': !imageForm.configured }"
        >
          <input
            :checked="imageForm.enableInDocParsing"
            type="checkbox"
            :disabled="!imageForm.configured"
            @change="handleImageUnderstandingToggle"
          />
          <span class="settings-toggle__track">
            <span class="settings-toggle__thumb" />
          </span>
          <span class="settings-toggle__label">启用文档图片理解</span>
        </label>
      </header>

      <form class="settings-form" @submit.prevent>
        <label class="settings-field">
          <span class="settings-field__label">视觉模型 API 地址</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="LinkOutline" />
            <input v-model="imageForm.apiBaseUrl" type="url" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
          </span>
        </label>

        <label class="settings-field">
          <span class="settings-field__label">视觉模型 API Key</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="KeyOutline" />
            <input
              v-model="imageForm.apiKey"
              class="settings-control__code"
              type="password"
              placeholder="sk-..."
            />
          </span>
        </label>

        <label class="settings-field">
          <span class="settings-field__label">模型名称</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="HardwareChipOutline" />
            <input v-model="imageForm.modelName" type="text" placeholder="qwen-vl-plus" autocomplete="off" />
          </span>
        </label>

        <label class="settings-field">
          <span class="settings-field__label">超时秒数</span>
          <span class="settings-control">
            <n-icon class="settings-control__icon" :component="TimerOutline" />
            <input v-model.number="imageForm.timeoutSeconds" type="number" min="5" max="300" />
          </span>
        </label>

        <p v-if="!imageForm.configured" class="settings-hint">
          保存 API Key 后即可启用图片理解开关。
        </p>

        <footer class="settings-actions">
          <button
            class="settings-button settings-button--secondary"
            type="button"
            :disabled="!canTestImageUnderstanding || testingImageUnderstanding"
            @click="testImageUnderstanding"
          >
            {{ testingImageUnderstanding ? '测试中' : '测试连接' }}
          </button>
          <button
            class="settings-button settings-button--primary"
            type="button"
            :disabled="savingImageUnderstanding"
            @click="saveImageUnderstanding"
          >
            {{ savingImageUnderstanding ? '保存中' : '保存设置' }}
          </button>
        </footer>
      </form>
    </section>

    <section class="settings-card ta-card">
      <header class="settings-card__header">
        <span class="settings-card__icon">
          <n-icon :component="ReaderOutline" />
        </span>
        <div class="settings-card__title">
          <h2>工具调用卡片叙事层输出</h2>
          <p>控制是否生成并显示工具调用卡片下方的执行说明；关闭后后端也不会调用叙事 LLM，动态 Agent 的过程说明仍会正常展示。</p>
        </div>
        <label
          class="settings-toggle"
          :class="{ 'settings-toggle--checked': settingsStore.toolCardNarrativeEnabled }"
          role="switch"
          :aria-checked="settingsStore.toolCardNarrativeEnabled"
          @click.prevent="handleNarrativeToggle"
        >
          <input
            :checked="settingsStore.toolCardNarrativeEnabled"
            type="checkbox"
            tabindex="-1"
            aria-hidden="true"
            style="position: absolute; opacity: 0; pointer-events: none;"
          />
          <span class="settings-toggle__track">
            <span class="settings-toggle__thumb" />
          </span>
          <span class="settings-toggle__label">启用叙事层输出</span>
        </label>
      </header>

      <p class="settings-hint">
        关闭后，工具调用卡片只保留状态、日志和执行结果；后端不会再为该卡片发起叙事 LLM 调用。
      </p>
    </section>

    <div
      v-if="showImageUnderstandingEnableConfirm"
      class="image-understanding-confirm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="image-understanding-confirm-title"
      @click.self="cancelImageUnderstandingEnable"
    >
      <div class="image-understanding-confirm__panel">
        <h3 id="image-understanding-confirm-title">启用文档图片理解？</h3>
        <p>
          启用后，系统会对需求文档中的流程图、原型图执行 OCR 和视觉模型理解，并把提取结果送入 LLM。
        </p>
        <div class="image-understanding-confirm__actions">
          <button class="settings-button settings-button--secondary" type="button" @click="cancelImageUnderstandingEnable">
            取消
          </button>
          <button class="settings-button settings-button--primary" type="button" @click="confirmImageUnderstandingEnable">
            确认启用
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useMessage, NIcon } from 'naive-ui'
import {
  HardwareChipOutline,
  ImageOutline,
  KeyOutline,
  LinkOutline,
  ReaderOutline,
  TimerOutline
} from '@vicons/ionicons5'
import { useSettingsStore } from '@/stores/settingsStore'
import { useImageUnderstandingStore, DEFAULT_BASE_URL as IMG_DEFAULT_BASE_URL, DEFAULT_MODEL as IMG_DEFAULT_MODEL } from '@/stores/imageUnderstandingStore'
import type { SettingsConfig } from '@/types'
import type { UpdateImageUnderstandingConfigRequest } from '@/types/imageUnderstanding'
import type { CapabilityType } from '@/api/settingsApi'
import { fetchCapabilityConfig } from '@/api/settingsApi'
import { ApiRequestError } from '@/api/request'
import CompanyRagSettingsCard from './CompanyRagSettingsCard.vue'

const settingsStore = useSettingsStore()
const imageStore = useImageUnderstandingStore()
const message = useMessage()
const form = reactive<SettingsConfig>({ ...settingsStore.settings })
const saving = ref(false)
const testingModel = ref(false)

// CE-01: 多能力模型配置状态
const selectedCapability = ref<CapabilityType>('chat')
const capabilityForm = reactive({
  apiBaseUrl: '',
  apiKey: '',
  modelName: '',
  timeoutSeconds: 120,
  contextWindowTokens: null as number | null,
  embeddingDimension: null as number | null,
  normalizeEmbeddings: false,
  preRerankLimit: null as number | null,
  rerankInstruction: '',
  scoreType: ''
})

function syncCapabilityForm(capabilityType: CapabilityType) {
  selectedCapability.value = capabilityType
  void fetchCapabilityConfig(capabilityType)
    .then((cfg) => {
      capabilityForm.apiBaseUrl = cfg.api_base_url ?? ''
      capabilityForm.apiKey = cfg.api_key_masked ?? ''
      capabilityForm.modelName = cfg.model_name ?? ''
      capabilityForm.timeoutSeconds = cfg.timeout_seconds ?? 120
      capabilityForm.contextWindowTokens = cfg.context_window_tokens ?? null
      capabilityForm.embeddingDimension = cfg.embedding_dimension ?? null
      capabilityForm.normalizeEmbeddings = cfg.normalize_embeddings ?? false
      capabilityForm.preRerankLimit = cfg.pre_rerank_limit ?? null
      capabilityForm.rerankInstruction = cfg.rerank_instruction ?? ''
      capabilityForm.scoreType = cfg.score_type ?? ''
    })
    .catch(() => {
      // 未配置则保持空表单
    })
}

watch(selectedCapability, (cap) => syncCapabilityForm(cap))

const imageForm = reactive({
  apiBaseUrl: IMG_DEFAULT_BASE_URL,
  apiKey: '',
  configured: false,
  modelName: IMG_DEFAULT_MODEL,
  timeoutSeconds: 60,
  enableInDocParsing: false
})
const savingImageUnderstanding = ref(false)
const testingImageUnderstanding = ref(false)
const showImageUnderstandingEnableConfirm = ref(false)

// Tool-card narrative visibility is user-facing. Narrative detail length is
// resolved from backend environment configuration.
const savingNarrative = ref(false)

const canTestModelConnection = computed(
  () => !!form.apiBaseUrl.trim() && !!form.apiKey.trim() && !!form.modelName.trim()
)
const modelConnectionConfig = computed<SettingsConfig>(() => ({
  ...form,
  apiBaseUrl: form.apiBaseUrl.trim(),
  apiKey: form.apiKey.includes('*') ? '' : form.apiKey.trim(),
  modelName: form.modelName.trim()
}))
const canTestImageUnderstanding = computed(
  () => !!imageForm.apiBaseUrl.trim() && !!imageForm.apiKey.trim() && !!imageForm.modelName.trim()
)

function syncImageForm() {
  const cfg = imageStore.config
  if (!cfg) {
    imageForm.apiKey = ''
    imageForm.configured = false
    return
  }
  imageForm.apiBaseUrl = cfg.api_base_url || IMG_DEFAULT_BASE_URL
  imageForm.apiKey = cfg.api_key_masked || ''
  imageForm.configured = Boolean(cfg.api_key_set)
  imageForm.modelName = cfg.model_name || IMG_DEFAULT_MODEL
  imageForm.timeoutSeconds = cfg.timeout_seconds || 60
  imageForm.enableInDocParsing = Boolean(cfg.enable_in_doc_parsing)
}

onMounted(async () => {
  try {
    await settingsStore.fetchSettings()
    Object.assign(form, settingsStore.settings)
  } catch (error) {
    const detail = error instanceof Error ? error.message : '请稍后重试'
    message.error(`读取设置失败：${detail}`)
  }
  try {
    await imageStore.fetchConfig()
    syncImageForm()
  } catch (error) {
    const detail = error instanceof Error ? error.message : '请稍后重试'
    message.error(`读取图片理解配置失败：${detail}`)
  }
})

watch(
  settingsStore.settings,
  (nextSettings) => {
    Object.assign(form, nextSettings)
  },
  { deep: true }
)

watch(
  () => imageStore.config,
  () => syncImageForm(),
  { deep: true }
)

function handleImageUnderstandingToggle(event: Event) {
  const target = event.target as HTMLInputElement
  if (!target.checked) {
    imageForm.enableInDocParsing = false
    return
  }
  target.checked = imageForm.enableInDocParsing
  showImageUnderstandingEnableConfirm.value = true
}

function confirmImageUnderstandingEnable() {
  imageForm.enableInDocParsing = true
  showImageUnderstandingEnableConfirm.value = false
}

function cancelImageUnderstandingEnable() {
  imageForm.enableInDocParsing = false
  showImageUnderstandingEnableConfirm.value = false
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    await settingsStore.saveSettings({ ...form })
    Object.assign(form, settingsStore.settings)
    message.success('设置已保存')
  } catch (error) {
    const detail = error instanceof Error ? error.message : '请稍后重试'
    message.error(`保存失败：${detail}`)
  } finally {
    saving.value = false
  }
}

async function testModel() {
  if (!canTestModelConnection.value) return
  testingModel.value = true
  try {
    const ok = await settingsStore.testModelConnection(modelConnectionConfig.value)
    if (ok) {
      message.success('模型服务连接成功')
    } else {
      message.error('模型服务连接失败')
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : '请稍后重试'
    message.error(`模型服务连接失败：${detail}`)
  } finally {
    testingModel.value = false
  }
}

async function saveImageUnderstanding() {
  if (savingImageUnderstanding.value) return
  if (!imageForm.apiBaseUrl.trim() || !imageForm.modelName.trim()) {
    message.error('请填写 API 地址和模型名称')
    return
  }
  savingImageUnderstanding.value = true
  try {
    const payload: {
      api_base_url: string
      model_name: string
      timeout_seconds: number
      enable_in_doc_parsing: boolean
      api_key?: string
    } = {
      api_base_url: imageForm.apiBaseUrl.trim(),
      model_name: imageForm.modelName.trim(),
      timeout_seconds: Number(imageForm.timeoutSeconds) || 60,
      enable_in_doc_parsing: imageForm.enableInDocParsing
    }
    if (imageForm.apiKey.trim() && !imageForm.apiKey.includes('*')) {
      payload.api_key = imageForm.apiKey.trim()
    }
    await imageStore.saveConfig(payload)
    imageForm.apiKey = ''
    syncImageForm()
    message.success('图片理解设置已保存')
  } catch (error) {
    const detail = error instanceof Error ? error.message : '请稍后重试'
    message.error(`保存失败：${detail}`)
  } finally {
    savingImageUnderstanding.value = false
  }
}

async function testImageUnderstanding() {
  if (!canTestImageUnderstanding.value) return
  testingImageUnderstanding.value = true
  try {
    const payload: UpdateImageUnderstandingConfigRequest = {
      api_base_url: imageForm.apiBaseUrl.trim(),
      api_key: imageForm.apiKey.trim(),
      model_name: imageForm.modelName.trim(),
      timeout_seconds: Number(imageForm.timeoutSeconds) || 60,
      enable_in_doc_parsing: imageForm.enableInDocParsing
    }
    const result = await imageStore.testConnection(payload)
    if (result.success) {
      message.success('图片理解服务连接成功')
    } else {
      message.error(`图片理解服务连接失败：${result.message ?? '未知错误'}`)
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : '请稍后重试'
    message.error(`图片理解服务连接失败：${detail}`)
  } finally {
    testingImageUnderstanding.value = false
  }
}

// ── Phase 2.9C narrative settings handlers ───────────────────────────────


async function handleNarrativeToggle() {
  if (savingNarrative.value) return
  const next = !settingsStore.toolCardNarrativeEnabled
  savingNarrative.value = true
  try {
    await settingsStore.saveToolCardNarrativeEnabled(next)
    message.success(next ? '已启用叙事层输出' : '已关闭叙事层输出')
  } catch (error) {
    const detail =
      error instanceof ApiRequestError
        ? `(${error.status}) ${error.message}`
        : error instanceof Error
          ? error.message
          : '请稍后重试'
    message.error(`保存失败：${detail}`)
  } finally {
    savingNarrative.value = false
  }
}
</script>

<style scoped>
.settings-stack {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.settings-card {
  padding: 24px;
  border: 0;
  border-radius: 12px;
}

.settings-card__header {
  display: flex;
  gap: 12px;
  align-items: center;
  padding-bottom: 16px;
  margin-bottom: 24px;
  border-bottom: 1px solid var(--ta-border);
}

.settings-card__icon {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  color: var(--ta-primary);
  background: var(--ta-surface-low);
  border-radius: var(--ta-radius-md);
}

.settings-card__title {
  min-width: 0;
}

.settings-card h2,
.settings-card p {
  margin: 0;
}

.settings-card h2 {
  color: var(--ta-text-strong);
  font-size: var(--text-card-title);
  font-weight: var(--font-semibold);
  line-height: 24px;
}

.settings-card p {
  color: #666666;
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: 21px;
}

.settings-form {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.settings-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.settings-field__label {
  color: #444444;
  font-size: var(--text-meta);
  font-weight: var(--font-medium);
  line-height: var(--leading-meta);
}

.settings-control {
  position: relative;
  display: flex;
  align-items: center;
  min-height: 38px;
  padding: 8px 12px;
  color: var(--ta-text-strong);
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-md);
  transition:
    border-color 0.18s ease,
    box-shadow 0.18s ease;
}

.settings-control:focus-within {
  border-color: var(--ta-primary);
  box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.08);
}

.settings-control__icon {
  flex: 0 0 auto;
  margin-right: 8px;
  color: var(--ta-outline);
  font-size: 20px;
}

.settings-control input {
  width: 100%;
  min-width: 0;
  padding: 0;
  margin: 0;
  color: var(--ta-text-strong);
  font: inherit;
  font-size: var(--text-md);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  background: transparent;
  border: 0;
  outline: 0;
}

.settings-control__code {
  font-family: var(--font-mono);
  font-size: var(--text-md);
  font-weight: var(--font-regular);
  font-variant-numeric: tabular-nums;
}

.settings-control__select {
  width: 100%;
  min-width: 0;
  padding: 0;
  margin: 0;
  color: var(--ta-text-strong);
  font: inherit;
  line-height: 20px;
  background: transparent;
  border: 0;
  outline: 0;
}

.settings-actions {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
  padding-top: 16px;
}

.settings-button {
  min-height: 36px;
  padding: 0 16px;
  font-size: var(--text-ui);
  font-weight: var(--font-medium);
  line-height: var(--leading-ui);
  cursor: pointer;
  border-radius: var(--ta-radius-md);
  transition:
    background 0.18s ease,
    border-color 0.18s ease,
    color 0.18s ease,
    opacity 0.18s ease;
}

.settings-button:disabled {
  cursor: default;
  opacity: 0.72;
}

.settings-button--secondary {
  color: var(--ta-primary);
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
}

.settings-button--secondary:hover:not(:disabled) {
  background: var(--ta-surface-low);
  border-color: var(--ta-primary);
}

.settings-button--primary {
  color: var(--ta-surface);
  background: var(--ta-primary);
  border: 1px solid var(--ta-primary);
}

.settings-button--primary:hover {
  background: var(--ta-primary-container);
  border-color: var(--ta-primary-container);
}

.settings-radio {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  border: 1px solid var(--ta-border-color, #dcdfe6);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 120ms ease, box-shadow 120ms ease;
}

.settings-radio--checked {
  border-color: var(--ta-color-primary, #2563eb);
  box-shadow: 0 0 0 1px var(--ta-color-primary, #2563eb) inset;
}

.settings-radio input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  margin: -1px;
}

.settings-radio__label {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.settings-radio__hint {
  font-size: 13px;
  color: var(--ta-text-muted, #6b7280);
}

.settings-field--radio-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border: 0;
  padding: 0;
  margin: 0;
}

.settings-field--radio-group[disabled] {
  opacity: 0.6;
  pointer-events: none;
}

.settings-toggle {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 12px;
  margin-left: auto;
  cursor: pointer;
}

.settings-toggle input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  padding: 0;
  margin: 0;
  opacity: 0;
  cursor: inherit;
}

.settings-toggle__track {
  position: relative;
  width: 44px;
  height: 24px;
  background: var(--ta-surface-variant);
  border-radius: 999px;
  transition: background 0.18s ease;
}

.settings-toggle__thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 20px;
  height: 20px;
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: 50%;
  transition: transform 0.18s ease;
}

.settings-toggle--checked .settings-toggle__track {
  background: var(--ta-primary);
}

.settings-toggle--checked .settings-toggle__thumb {
  transform: translateX(20px);
}

.settings-toggle input:focus-visible + .settings-toggle__track {
  box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.1);
}

.settings-toggle__label {
  color: var(--ta-outline);
  font-size: var(--text-meta);
  font-weight: var(--font-medium);
  line-height: var(--leading-meta);
}

.settings-toggle--disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.settings-hint {
  margin: -4px 0 0;
  color: var(--ta-outline);
  font-size: var(--text-meta);
  line-height: var(--leading-meta);
}

.image-understanding-confirm {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(248, 250, 252, 0.72);
  backdrop-filter: blur(8px);
}

.image-understanding-confirm__panel {
  width: min(100%, 420px);
  padding: 24px;
  color: var(--ta-text-strong);
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: 14px;
  box-shadow: 0 18px 50px rgba(15, 23, 42, 0.16);
}

.image-understanding-confirm__panel h3 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  line-height: 26px;
}

.image-understanding-confirm__panel p {
  margin: 14px 0 0;
  color: var(--ta-text-muted);
  font-size: 14px;
  line-height: 22px;
}

.image-understanding-confirm__actions {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
  margin-top: 28px;
}

@media (max-width: 640px) {
  .settings-card {
    padding: 18px;
  }

  .settings-card__header {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .settings-toggle {
    width: 100%;
    margin-left: 48px;
  }

  .settings-actions {
    flex-wrap: wrap;
  }

  .settings-button {
    flex: 1 1 120px;
  }
}
</style>
