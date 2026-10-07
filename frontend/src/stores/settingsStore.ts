/**
 * Settings store — model / knowledge-base / upload config.
 */

import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import type { SettingsConfig } from '@/types'
import * as settingsApi from '@/api/settingsApi'
import type { NarrativeDetailLevel, NarrativeSettings } from '@/api/settingsApi'
import { ApiRequestError } from '@/api/request'

// Cache dedupe constants — keep in sync with backend Phase 0 design.
// ``MODEL_SETTINGS_FRESH_MS`` is the in-memory "fresh enough" window
// during which a re-enter to ``ensureModelSettings`` returns the
// last-fetched value without a new HTTP call.
const MODEL_SETTINGS_FRESH_MS = 10_000
// ``FETCH_SETTINGS_FRESH_MS`` covers the full ``fetchSettings``
// bundle (knowledge + upload + model + narrative).  Anything
// fresher than this skips the entire GET cycle.
const FETCH_SETTINGS_FRESH_MS = 10_000

const emptySettings: SettingsConfig = {
  apiBaseUrl: '',
  apiKey: '',
  modelName: '',
  timeoutSeconds: 120,
  knowledgeBaseUrl: '',
  knowledgeCollection: '',
  enableKnowledgeBase: false,
  maxFileSizeMb: 30,
  maxFilesPerConversation: 6,
  allowedExtensions: [],
  capabilityType: 'chat',
  contextWindowTokens: null,
  contextWindowK: null,
  embeddingDimension: null,
  normalizeEmbeddings: null
}

export const useSettingsStore = defineStore('settings', () => {
  const settings = reactive<SettingsConfig>({ ...emptySettings })
  const loading = ref(false)
  const modelSettingsChecked = ref(false)
  const modelSettingsMissing = ref(false)
  const narrativeDetailLevel = ref<NarrativeDetailLevel>('standard')
  const toolCardNarrativeEnabled = ref(true)
  const hasModelSettings = computed(
    () => !!settings.apiBaseUrl.trim() && !!settings.apiKey.trim() && !!settings.modelName.trim()
  )

  // ── Phase 0 — request-dedupe cache state ─────────────────────
  // ``ensureModelSettings`` / ``fetchModelSettings`` is invoked from
  // AppLayout (route watcher) AND SettingsForm (onMounted) AND
  // store-eagerly on first access.  Without dedupe three concurrent
  // GET /api/settings/model calls fire.  We use a single in-flight
  // Promise + a freshness timestamp so the second and third callers
  // reuse the first result instead of re-issuing HTTP.
  let ensureModelInFlight: Promise<boolean> | null = null
  let ensureModelFetchedAt = 0

  let fetchSettingsInFlight: Promise<void> | null = null
  let fetchSettingsFetchedAt = 0

  function isMissingModelSettingsError(error: unknown): boolean {
    return error instanceof ApiRequestError && (error.status === 404 || error.code === 40400)
  }

  function clearModelSettings() {
    settings.apiBaseUrl = ''
    settings.apiKey = ''
    settings.modelName = ''
    settings.timeoutSeconds = emptySettings.timeoutSeconds
  }

  function applyModelSettings(config: Partial<SettingsConfig>) {
    Object.assign(settings, config)
    modelSettingsMissing.value = !hasModelSettings.value
    modelSettingsChecked.value = true
  }

  async function ensureModelSettings(force = false): Promise<boolean> {
    // Phase 0 — in-flight + freshness dedupe.
    // - If a fetch is currently in progress, await the same Promise.
    // - If the last successful fetch is fresher than
    //   ``MODEL_SETTINGS_FRESH_MS`` AND ``modelSettingsChecked`` is
    //   already true, skip the HTTP call and return the cached result.
    // - ``force=true`` bypasses both protections (used after user
    //   explicit "save" or "test connection").
    if (!force) {
      if (ensureModelInFlight) return ensureModelInFlight
      if (
        modelSettingsChecked.value
        && ensureModelFetchedAt > 0
        && Date.now() - ensureModelFetchedAt < MODEL_SETTINGS_FRESH_MS
      ) {
        return hasModelSettings.value
      }
    }

    const promise = (async () => {
      loading.value = true
      try {
        const model = await settingsApi.fetchModelSettings()
        applyModelSettings(model)
        ensureModelFetchedAt = Date.now()
        return hasModelSettings.value
      } catch (error) {
        if (!isMissingModelSettingsError(error)) throw error
        clearModelSettings()
        modelSettingsMissing.value = true
        modelSettingsChecked.value = true
        return false
      } finally {
        loading.value = false
      }
    })()

    ensureModelInFlight = promise
    try {
      return await promise
    } finally {
      ensureModelInFlight = null
    }
  }

  async function fetchSettings(force = false) {
    // Phase 0 — dedupe the full settings bundle fetch (knowledge +
    // upload + model + narrative) using the same in-flight Promise +
    // freshness window pattern.  Within ``FETCH_SETTINGS_FRESH_MS``
    // concurrent callers reuse the same Promise.
    if (!force) {
      if (fetchSettingsInFlight) return fetchSettingsInFlight
      if (
        fetchSettingsFetchedAt > 0
        && Date.now() - fetchSettingsFetchedAt < FETCH_SETTINGS_FRESH_MS
      ) {
        return
      }
    }

    const promise = (async () => {
      loading.value = true
      try {
        const [knowledgeBase, upload] = await Promise.all([
          settingsApi.fetchKnowledgeBaseSettings(),
          settingsApi.fetchUploadSettings()
        ])
        // Reuse the inner dedupe for the model call.
        await ensureModelSettings(force)
        Object.assign(settings, knowledgeBase, upload)
        try {
          const narrative = await settingsApi.fetchNarrativeSettings()
          narrativeDetailLevel.value = narrative.detail_level
          toolCardNarrativeEnabled.value = narrative.enabled
        } catch (error) {
          // Narrative settings are additive — older backends (Phase
          // 2.9A/B without 2.9C) won't expose this endpoint, so we
          // silently fall back to ``standard`` and keep using the
          // existing 2.9A behaviour.
          if (!(error instanceof ApiRequestError) || error.status !== 404) {
            // Real errors propagate so the operator sees them.
            if (
              error instanceof ApiRequestError
              && typeof error.status === 'number'
              && error.status >= 500
            ) {
              throw error
            }
          }
          narrativeDetailLevel.value = 'standard'
          toolCardNarrativeEnabled.value = true
        }
        fetchSettingsFetchedAt = Date.now()
      } finally {
        loading.value = false
      }
    })()

    fetchSettingsInFlight = promise
    try {
      await promise
    } finally {
      fetchSettingsInFlight = null
    }
  }

  async function saveSettings(config: SettingsConfig) {
    loading.value = true
    try {
      const saved = await settingsApi.saveAllSettings(config)
      Object.assign(settings, saved)
      modelSettingsMissing.value = !hasModelSettings.value
      modelSettingsChecked.value = true
    } finally {
      loading.value = false
    }
  }

  function updateSettings(config: SettingsConfig) {
    Object.assign(settings, config)
  }

  async function testModelConnection(override?: Partial<SettingsConfig>): Promise<boolean> {
    const candidate: SettingsConfig = { ...settings, ...(override ?? {}) }
    const result = await settingsApi.testModelConnection(candidate)
    return result.ok
  }

  async function testKnowledgeBaseConnection(override?: Partial<SettingsConfig>): Promise<boolean> {
    const candidate: SettingsConfig = { ...settings, ...(override ?? {}) }
    const result = await settingsApi.testKnowledgeBaseConnection(candidate)
    return result.ok
  }

  async function saveToolCardNarrativeEnabled(enabled: boolean): Promise<NarrativeSettings> {
    const persisted = await settingsApi.saveNarrativeSettings(enabled)
    narrativeDetailLevel.value = persisted.detail_level
    toolCardNarrativeEnabled.value = persisted.enabled
    return persisted
  }

  // Eagerly load on first store access
  fetchSettings()

  return {
    settings,
    loading,
    hasModelSettings,
    modelSettingsChecked,
    modelSettingsMissing,
    narrativeDetailLevel,
    toolCardNarrativeEnabled,
    fetchSettings,
    ensureModelSettings,
    saveSettings,
    updateSettings,
    testModelConnection,
    testKnowledgeBaseConnection,
    saveToolCardNarrativeEnabled
  }
})
