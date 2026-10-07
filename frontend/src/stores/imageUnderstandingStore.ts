import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import * as imageUnderstandingApi from '@/api/imageUnderstandingApi'
import type {
  ImageUnderstandingConfig,
  ImageUnderstandingTestResult,
  UpdateImageUnderstandingConfigRequest
} from '@/types/imageUnderstanding'

export const DEFAULT_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
export const DEFAULT_MODEL = 'qwen-vl-plus'

function unconfigured(): ImageUnderstandingConfig {
  return {
    config_id: null,
    api_base_url: DEFAULT_BASE_URL,
    api_key_masked: '',
    api_key_set: false,
    model_name: DEFAULT_MODEL,
    timeout_seconds: 60,
    max_tokens: null,
    enable_in_doc_parsing: false,
    last_test_status: null,
    last_test_message: null,
    last_test_at: null,
    updated_at: null
  }
}

export const useImageUnderstandingStore = defineStore('imageUnderstanding', () => {
  const config = ref<ImageUnderstandingConfig | null>(null)
  const lastTestResult = ref<ImageUnderstandingTestResult | null>(null)

  const loading = reactive({
    config: false,
    mutating: false,
    testing: false
  })

  const error = ref<string>('')

  const isConfigured = computed(() => Boolean(config.value?.api_key_set))
  const isEnabled = computed(() => Boolean(config.value?.enable_in_doc_parsing))

  async function fetchConfig(): Promise<ImageUnderstandingConfig> {
    loading.config = true
    error.value = ''
    try {
      const next = await imageUnderstandingApi.getImageUnderstandingConfig()
      config.value = next
      return next
    } catch (err) {
      config.value = unconfigured()
      error.value = err instanceof Error ? err.message : '加载图片理解配置失败'
      throw err
    } finally {
      loading.config = false
    }
  }

  async function saveConfig(payload: UpdateImageUnderstandingConfigRequest): Promise<void> {
    loading.mutating = true
    error.value = ''
    try {
      await imageUnderstandingApi.updateImageUnderstandingConfig(payload)
      await fetchConfig()
    } catch (err) {
      error.value = err instanceof Error ? err.message : '保存图片理解配置失败'
      throw err
    } finally {
      loading.mutating = false
    }
  }

  async function testConnection(
    override?: UpdateImageUnderstandingConfigRequest
  ): Promise<ImageUnderstandingTestResult> {
    loading.testing = true
    error.value = ''
    try {
      const result = await imageUnderstandingApi.testImageUnderstandingConnection(
        override
      )
      lastTestResult.value = result
      return result
    } catch (err) {
      error.value = err instanceof Error ? err.message : '测试图片理解连接失败'
      throw err
    } finally {
      loading.testing = false
    }
  }

  function reset() {
    config.value = null
    lastTestResult.value = null
    error.value = ''
  }

  return {
    config,
    lastTestResult,
    loading,
    error,
    isConfigured,
    isEnabled,
    fetchConfig,
    saveConfig,
    testConnection,
    reset
  }
})
