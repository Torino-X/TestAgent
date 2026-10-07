import { reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import * as api from '@/api/templateApi'
import type { TemplateCategory, TemplateMarketItem, TemplateSort, TemplateSourceType, TemplateUploadPayload, UserTemplateItem } from '@/types/template'

export const useTemplateStore = defineStore('templates', () => {
  const marketItems = ref<TemplateMarketItem[]>([])
  const myItems = ref<UserTemplateItem[]>([])
  const totals = reactive({ market: 0, mine: 0 })
  const loading = reactive({ market: false, mine: false, mutation: false })
  const errors = reactive({ market: '', mine: '', mutation: '' })

  const errorText = (error: unknown) => error instanceof Error ? error.message : '模板操作失败，请稍后重试。'

  async function loadMarket(params: { q?: string; category?: TemplateCategory; sort?: TemplateSort } = {}) {
    loading.market = true; errors.market = ''
    try { const page = await api.fetchMarketTemplates(params); marketItems.value = page.items; totals.market = page.total; return page }
    catch (error) { errors.market = errorText(error); throw error }
    finally { loading.market = false }
  }
  async function loadMine(params: { q?: string; category?: TemplateCategory; sourceType?: TemplateSourceType } = {}) {
    loading.mine = true; errors.mine = ''
    try { const page = await api.fetchMyTemplates(params); myItems.value = page.items; totals.mine = page.total; return page }
    catch (error) { errors.mine = errorText(error); throw error }
    finally { loading.mine = false }
  }
  async function save(item: TemplateMarketItem) {
    loading.mutation = true; errors.mutation = ''
    try { const result = await api.saveMarketTemplate(item.templateId); item.isSaved = true; if (!result.already_saved) item.saveCount += 1; return result }
    catch (error) { errors.mutation = errorText(error); throw error }
    finally { loading.mutation = false }
  }
  async function upload(payload: TemplateUploadPayload) {
    loading.mutation = true; errors.mutation = ''
    try { const item = await api.uploadTemplate(payload); myItems.value = [item, ...myItems.value]; totals.mine += 1; return item }
    catch (error) { errors.mutation = errorText(error); throw error }
    finally { loading.mutation = false }
  }
  async function setPublished(item: UserTemplateItem, publish: boolean) {
    loading.mutation = true; errors.mutation = ''
    try { publish ? await api.publishTemplate(item.id) : await api.unpublishTemplate(item.id); item.visibility = publish ? 'public' : 'private' }
    catch (error) { errors.mutation = errorText(error); throw error }
    finally { loading.mutation = false }
  }
  async function remove(item: UserTemplateItem) {
    loading.mutation = true; errors.mutation = ''
    try { await api.removeTemplate(item.id); myItems.value = myItems.value.filter((candidate) => candidate.id !== item.id); totals.mine = Math.max(0, totals.mine - 1) }
    catch (error) { errors.mutation = errorText(error); throw error }
    finally { loading.mutation = false }
  }
  return { marketItems, myItems, totals, loading, errors, loadMarket, loadMine, save, upload, setPublished, remove }
})
