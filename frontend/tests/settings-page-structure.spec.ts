import { describe, expect, it } from 'vitest'
import appLayoutSource from '../src/components/layout/AppLayout.vue?raw'
import settingsFormSource from '../src/components/settings/SettingsForm.vue?raw'
import settingsStoreSource from '../src/stores/settingsStore.ts?raw'
import settingsViewSource from '../src/views/SettingsView.vue?raw'

describe('settings page Stitch restoration', () => {
  it('keeps the system settings page and model form structure', () => {
    expect(settingsViewSource).toContain('配置 TestAgent 的核心运行参数与依赖服务。')
    expect(settingsFormSource).toContain('设置底层语言模型 API 信息。')
    expect(settingsFormSource).toContain('图片理解配置')
    expect(settingsFormSource).toContain('API URL')
    expect(settingsFormSource).toContain('API KEY')
    expect(settingsFormSource).toContain('模型名称')
    expect(settingsFormSource).toContain('settings-toggle--checked')
  })

  it('does not render knowledge-base configuration in system settings', () => {
    expect(settingsFormSource).not.toContain('知识库配置')
    expect(settingsFormSource).not.toContain('连接向量数据库以启用 RAG 功能。')
    expect(settingsFormSource).not.toContain('向量库 API 地址')
    expect(settingsFormSource).not.toContain('集合 ID (Collection ID)')
    expect(settingsFormSource).not.toContain('启用检索')
    expect(settingsFormSource).not.toContain('BookOutline')
    expect(settingsFormSource).not.toContain('CloudOutline')
    expect(settingsFormSource).not.toContain('ServerOutline')
    expect(settingsFormSource).not.toContain('form.enableKnowledgeBase')
    expect(settingsFormSource).not.toContain('form.knowledgeBaseUrl')
    expect(settingsFormSource).not.toContain('form.knowledgeCollection')
    expect(settingsFormSource).not.toContain('testKnowledge')
  })

  it('keeps the settings form scoped to the Stitch fields', () => {
    expect(settingsFormSource).not.toContain('超时时间')
    expect(settingsFormSource).not.toContain('单文件上传大小限制')
    expect(settingsFormSource).not.toContain('最大上传文件数')
  })

  it('lets users type the model name directly', () => {
    expect(settingsFormSource).toContain('<input v-model="form.modelName"')
    expect(settingsFormSource).not.toContain('<select v-model="form.modelName"')
  })

  it('prompts users to configure model settings before using protected pages', () => {
    expect(settingsStoreSource).toContain('ensureModelSettings')
    expect(settingsStoreSource).toContain('hasModelSettings')
    expect(settingsStoreSource).toContain('isMissingModelSettingsError')
    expect(appLayoutSource).toContain('showModelConfigRequiredDialog')
    expect(appLayoutSource).toContain('delete-confirm')
    expect(appLayoutSource).toContain("router.push('/settings')")
  })

  it('enables model connection testing only for the current complete form values', () => {
    expect(settingsFormSource).toContain('canTestModelConnection')
    expect(settingsFormSource).toContain(':disabled="!canTestModelConnection || testingModel"')
    expect(settingsFormSource).toContain('const modelConnectionConfig')
    expect(settingsFormSource).toContain('settingsStore.testModelConnection(modelConnectionConfig.value)')
  })

  it('renders saved image understanding API key as a password value', () => {
    expect(settingsFormSource).toContain('type="password"')
    expect(settingsFormSource).toContain('imageForm.apiKey = cfg.api_key_masked ||')
    expect(settingsFormSource).not.toContain(':placeholder="imageForm.apiKeyMasked ||')
  })

  it('requires confirmation before enabling document image understanding', () => {
    expect(settingsFormSource).toContain('showImageUnderstandingEnableConfirm')
    expect(settingsFormSource).toContain('@change="handleImageUnderstandingToggle"')
    expect(settingsFormSource).toContain('confirmImageUnderstandingEnable')
    expect(settingsFormSource).toContain('cancelImageUnderstandingEnable')
    expect(settingsFormSource).toContain('image-understanding-confirm')
    expect(settingsFormSource).not.toContain('v-model="imageForm.enableInDocParsing" type="checkbox"')
  })

  it('keeps a focused image-understanding checkbox inside its visible toggle', () => {
    expect(settingsFormSource).toMatch(/\.settings-toggle\s*\{[\s\S]*?position:\s*relative;/)
    expect(settingsFormSource).toMatch(/\.settings-toggle input\s*\{[\s\S]*?inset:\s*0;/)
    expect(settingsFormSource).toMatch(/\.settings-toggle input\s*\{[\s\S]*?width:\s*100%;/)
    expect(settingsFormSource).toMatch(/\.settings-toggle input\s*\{[\s\S]*?height:\s*100%;/)
  })
})
