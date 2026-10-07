<template>
  <AppLayout>
    <section class="templates-page" aria-labelledby="templates-title">
      <div class="templates-content">
        <header class="templates-header">
          <div class="templates-heading">
            <h1 id="templates-title">模板市场</h1>
            <p>发现、保存并复用高质量测试模板</p>
          </div>
          <div class="templates-header__actions">
            <label class="templates-search">
              <n-icon :component="SearchOutline" />
              <input v-model="query" type="search" placeholder="搜索模板名称、用例或标签…" aria-label="搜索模板" />
            </label>
            <button class="templates-upload" type="button" @click="uploadVisible = true">
              <n-icon :component="CloudUploadOutline" />上传模板
            </button>
          </div>
        </header>

        <div class="templates-tabs">
          <button type="button" :class="{ active: activeTab === 'market' }" @click="activeTab = 'market'">模板市场</button>
          <button type="button" :class="{ active: activeTab === 'mine' }" @click="activeTab = 'mine'">我的模板</button>
          <span v-if="activeTab === 'market'">已收录 {{ templateStore.totals.market }} 个测试模板</span>
        </div>

        <div class="templates-toolbar">
          <nav aria-label="模板分类">
            <button v-for="item in TEMPLATE_CATEGORIES" :key="item.value" type="button" :class="{ active: category === item.value }" @click="category = item.value">{{ item.label }}</button>
          </nav>
          <AppSelect
            v-if="activeTab === 'market'"
            :model-value="sort"
            :options="sortOptions"
            aria-label="模板排序"
            :min-width="112"
            placement="bottom-end"
            @update:model-value="sort = $event as TemplateSort"
          />
          <AppSelect
            v-else
            :model-value="sourceType"
            :options="sourceOptions"
            aria-label="模板来源"
            :min-width="124"
            placement="bottom-end"
            @update:model-value="sourceType = $event as TemplateSourceType"
          />
        </div>

        <div v-if="loading" class="templates-grid templates-skeleton" role="status" aria-label="正在加载模板">
          <article v-for="i in 6" :key="i" class="template-card template-card--skeleton">
            <div class="template-card__cover-skeleton"><span></span></div>
            <div class="template-card__content"><i></i><strong></strong><p></p><small></small></div>
          </article>
        </div>

        <div v-else-if="error" class="templates-state">
          <n-icon :component="AlertCircleOutline" :size="34" />
          <strong>模板加载失败</strong><p>{{ error }}</p><button type="button" @click="reload">重试</button>
        </div>

        <div v-else-if="visibleItems.length" class="templates-grid" role="list">
          <article
            v-for="item in visibleItems"
            :key="item.id"
            class="template-card template-card--clickable"
            role="listitem"
            tabindex="0"
            :aria-label="`查看模板：${item.name}`"
            @click="openPreview(item)"
            @keydown.enter="openPreview(item)"
            @keydown.space.prevent="openPreview(item)"
          >
            <TemplateCoverPreview :item="item" />
            <div class="template-card__content">
              <div class="template-card__tags">
                <span>{{ templateCategoryLabel(item.categoryCode) }}</span>
                <span v-if="item.tags[0]">{{ item.tags[0] }}</span>
                <span v-if="activeTab === 'mine' && 'visibility' in item" :class="{ public: item.visibility === 'public' }">{{ item.visibility === 'public' ? '已发布' : '未发布' }}</span>
              </div>
              <h2>{{ item.name }}</h2>
              <p class="template-card__description">{{ item.description || '暂无描述' }}</p>
              <div class="template-card__details">
                <span>{{ item.author.displayName }} · {{ formatDate(item.publishedAt || item.updatedAt) }}</span>
                <span>v{{ item.versionNo }} · {{ formatSize(item.fileSize) }}</span>
              </div>
              <div class="template-card__footer">
                <span class="template-card__saves"><n-icon :component="BookmarkOutline" />{{ item.saveCount }} 次保存</span>
                <div class="template-card__actions" @click.stop @keydown.stop>
                  <template v-if="activeTab === 'market' && 'isSaved' in item">
                    <button type="button" class="save" :class="{ saved: item.isSaved }" :disabled="item.isSaved || pending.has(item.id)" @click="save(item)">
                      <n-icon :component="item.isSaved ? CheckmarkOutline : BookmarkOutline" />{{ item.isSaved ? '已保存' : '保存模板' }}
                    </button>
                  </template>
                  <template v-else-if="'sourceType' in item">
                    <button class="use" type="button" :disabled="pending.has(item.id)" @click="useInNewConversation(item)">使用模板</button>
                    <n-dropdown :options="myActions(item)" :menu-props="templateActionMenuProps" trigger="click" placement="bottom-end" :show-arrow="false" @select="(key) => handleMyAction(String(key), item)">
                      <button class="more" type="button" aria-label="更多模板操作"><n-icon :component="EllipsisHorizontal" /></button>
                    </n-dropdown>
                  </template>
                </div>
              </div>
            </div>
          </article>
        </div>

        <div v-else class="templates-state templates-state--empty">
          <n-icon :component="GridOutline" :size="36" />
          <strong>{{ query ? '没有找到匹配的模板' : activeTab === 'market' ? '模板市场暂时为空' : '还没有我的模板' }}</strong>
          <p>{{ activeTab === 'mine' ? '从模板市场保存模板，或上传你自己的模板。' : '稍后再来看看新的测试模板。' }}</p>
          <button v-if="activeTab === 'mine'" type="button" @click="uploadVisible = true">上传模板</button>
        </div>
      </div>
    </section>
  </AppLayout>
  <TemplateUploadDialog :show="uploadVisible" :submitting="templateStore.loading.mutation" @close="uploadVisible = false" @submit="submitUpload" />
</template>

<script setup lang="ts">
import { computed, h, ref, watch, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NDropdown, NIcon, useMessage, type DropdownOption } from 'naive-ui'
import { AlertCircleOutline, BookmarkOutline, CheckmarkOutline, CloudUploadOutline, DownloadOutline, EllipsisHorizontal, EyeOffOutline, EyeOutline, GridOutline, SearchOutline, TrashOutline } from '@vicons/ionicons5'
import AppLayout from '@/components/layout/AppLayout.vue'
import AppSelect from '@/components/common/AppSelect.vue'
import TemplateCoverPreview from '@/components/templates/TemplateCoverPreview.vue'
import TemplateUploadDialog from '@/components/templates/TemplateUploadDialog.vue'
import { downloadTemplate, useTemplate } from '@/api/templateApi'
import { useTemplateStore } from '@/stores/templateStore'
import { useConversationStore } from '@/stores/conversationStore'
import { TEMPLATE_CATEGORIES, templateCategoryLabel, type TemplateCategory, type TemplateMarketItem, type TemplateSort, type TemplateSourceType, type TemplateUploadPayload, type UserTemplateItem } from '@/types/template'

const router = useRouter()
const route = useRoute()
const message = useMessage()
const templateStore = useTemplateStore()
const conversationStore = useConversationStore()
const activeTab = ref<'market' | 'mine'>(route.query.tab === 'mine' ? 'mine' : 'market')
const query = ref('')
const category = ref<TemplateCategory>('all')
const sort = ref<TemplateSort>('newest')
const sourceType = ref<TemplateSourceType>('all')
const sortOptions = [
  { label: '最新发布', value: 'newest' },
  { label: '最多保存', value: 'saves' }
] satisfies Array<{ label: string; value: TemplateSort }>
const sourceOptions = [
  { label: '全部来源', value: 'all' },
  { label: '我上传的', value: 'owner' },
  { label: '市场保存', value: 'market_saved' }
] satisfies Array<{ label: string; value: TemplateSourceType }>
const uploadVisible = ref(false)
const pending = ref(new Set<string>())
let reloadTimer: ReturnType<typeof setTimeout> | undefined

const visibleItems = computed(() => activeTab.value === 'market' ? templateStore.marketItems : templateStore.myItems)
const loading = computed(() => activeTab.value === 'market' ? templateStore.loading.market : templateStore.loading.mine)
const error = computed(() => activeTab.value === 'market' ? templateStore.errors.market : templateStore.errors.mine)

watch([activeTab, query, category, sort, sourceType], () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(() => void reload(), 220) }, { immediate: true })
async function reload() { try { if (activeTab.value === 'market') await templateStore.loadMarket({ q: query.value, category: category.value, sort: sort.value }); else await templateStore.loadMine({ q: query.value, category: category.value, sourceType: sourceType.value }) } catch { /* inline state */ } }
function openPreview(item: TemplateMarketItem | UserTemplateItem) {
  if ('sourceType' in item) {
    void router.push({ name: 'my-template-preview', params: { userTemplateId: item.id }, query: { from: 'templates', tab: 'mine' } })
    return
  }
  void router.push({ name: 'template-preview', params: { templateId: item.templateId }, query: { from: 'templates' } })
}
function markPending(id: string, value: boolean) { const next = new Set(pending.value); value ? next.add(id) : next.delete(id); pending.value = next }
async function save(item: TemplateMarketItem) { markPending(item.id, true); try { await templateStore.save(item); message.success('模板已成功保存，可前往“我的模板”中使用。') } catch (e) { message.error(errorText(e)) } finally { markPending(item.id, false) } }
async function submitUpload(payload: TemplateUploadPayload) { try { await templateStore.upload(payload); uploadVisible.value = false; activeTab.value = 'mine'; message.success('模板已上传') } catch (e) { message.error(errorText(e)) } }
async function useInNewConversation(item: UserTemplateItem) { markPending(item.id, true); try { const conversation = await conversationStore.startNewConversation(); const result = await useTemplate(item.id, conversation.id); conversationStore.mergeConversationSummary(result.conversation); conversationStore.queueInitialAttachments(result.conversation.id, [result.uploadedFile]); await router.push(`/chat/${encodeURIComponent(result.conversation.id)}`); message.success('模板已加入会话，请确认后发送。') } catch (e) { message.error(errorText(e)) } finally { markPending(item.id, false) } }
function templateAction(label: string, key: string, icon: Component, danger = false): DropdownOption {
  return {
    key,
    props: danger ? { class: 'template-action-option--danger' } : undefined,
    label: () => h('span', { class: ['template-action-option', danger ? 'template-action-option--danger' : ''] }, [
      h('span', { class: 'template-action-option__icon' }, [h(NIcon, { component: icon, size: 18 })]),
      h('span', { class: 'template-action-option__label' }, label)
    ])
  }
}
const templateActionMenuProps = () => ({ class: 'template-action-menu' })
function myActions(item: UserTemplateItem): DropdownOption[] { const owner = item.sourceType === 'owner'; return [
  templateAction('下载模板', 'download', DownloadOutline),
  ...(owner ? [templateAction(item.visibility === 'public' ? '取消发布' : '发布到市场', item.visibility === 'public' ? 'unpublish' : 'publish', item.visibility === 'public' ? EyeOffOutline : EyeOutline)] : []),
  { type: 'divider', key: 'divider' }, templateAction(owner ? '删除模板' : '从我的模板移除', 'remove', TrashOutline, true)
] }
async function handleMyAction(action: string, item: UserTemplateItem) { markPending(item.id, true); try { if (action === 'download') await downloadTemplate(item); else if (action === 'publish' || action === 'unpublish') { await templateStore.setPublished(item, action === 'publish'); message.success(action === 'publish' ? '模板已发布' : '模板已取消发布') } else if (action === 'remove' && window.confirm(item.sourceType === 'owner' ? '确定删除这个模板吗？' : '确定从“我的模板”中移除吗？')) { await templateStore.remove(item); message.success('模板已移除') } } catch (e) { message.error(errorText(e)) } finally { markPending(item.id, false) } }
function errorText(error: unknown) { return error instanceof Error ? error.message : '操作失败，请稍后重试。' }
function formatSize(bytes: number) { return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB` }
function formatDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date) }
</script>

<style scoped>
.templates-page{flex:1;min-width:0;height:100%;overflow:auto;color:#171717;background:#fff}.templates-content{width:min(770px,calc(100% - 48px));margin:0 auto;padding:112px 0 70px}.templates-header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px}.templates-header h1{margin:0;font-size:29px;font-weight:560;letter-spacing:-.8px}.templates-header p{margin:7px 0 0;color:#868686;font-size:13px}.templates-header__actions{display:flex;gap:9px}.templates-search{display:flex;align-items:center;gap:7px;width:210px;height:36px;padding:0 10px;background:#fafafa;border:1px solid #e5e5e5;border-radius:9px}.templates-search input{min-width:0;flex:1;background:transparent;border:0;outline:0}.templates-upload{display:flex;align-items:center;gap:7px;height:36px;padding:0 14px;color:#fff;font-weight:520;background:#171717;border:0;border-radius:9px;cursor:pointer}.templates-tabs{display:flex;gap:25px;margin-top:34px;border-bottom:1px solid #e9e9e9}.templates-tabs button{position:relative;padding:0 1px 12px;color:#858585;font-size:14px;background:transparent;border:0;cursor:pointer}.templates-tabs button.active{color:#171717;font-weight:560}.templates-tabs button.active::after{position:absolute;right:0;bottom:-1px;left:0;height:2px;background:#171717;content:''}.templates-toolbar{display:flex;align-items:center;justify-content:space-between;padding:15px 0 8px}.templates-toolbar nav{display:flex;gap:4px}.templates-toolbar nav button{padding:6px 10px;color:#737373;background:transparent;border:0;border-radius:7px;cursor:pointer}.templates-toolbar nav button.active{color:#171717;background:#f1f1ef}.templates-list{border-top:1px solid #ededed}.template-row{display:grid;grid-template-columns:50px minmax(0,1fr) auto;gap:14px;align-items:center;min-height:100px;padding:13px 8px;border-bottom:1px solid #ededed;transition:background .15s ease}.template-row--clickable{cursor:pointer}.template-row:hover{background:#fafafa}.template-row__icon{display:grid;width:45px;height:45px;color:#38514a;font-size:9px;background:#e9f1ed;border-radius:10px;place-items:center}.template-row__icon--xlsx{color:#3d563c;background:#eaf2e7}.template-row__body{min-width:0}.template-row__title{display:flex;align-items:center;gap:7px}.template-row h2{max-width:380px;margin:0;overflow:hidden;font-size:15px;font-weight:570;text-overflow:ellipsis;white-space:nowrap}.template-row__title span{padding:2px 6px;color:#777;font-size:10px;background:#f1f1ef;border-radius:5px}.template-row__title span.public{color:#315f46;background:#eaf5ee}.template-row__body>p{margin:7px 0;color:#686868;font-size:13px;line-height:1.45;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.template-row__meta{display:flex;gap:8px;color:#9a9a9a;font-size:11px}.template-row__meta span{display:flex;align-items:center;gap:3px}.template-row__actions{display:flex;gap:6px;align-items:center}.template-row__actions button{height:34px;padding:0 12px;border-radius:8px;cursor:pointer}.template-row__actions .save{display:flex;align-items:center;gap:5px;color:#333;background:#fff;border:1px solid #dcdcdc}.template-row__actions .save.saved{color:#777;background:#f4f4f2}.template-row__actions .use{color:#fff;background:#171717;border:1px solid #171717}.template-row__actions .more{width:34px;padding:0;color:#555;background:#fff;border:1px solid #dedede}.template-row__actions button:disabled{cursor:not-allowed;opacity:.55}.templates-skeleton{border-top:1px solid #eee}.templates-skeleton>div{display:grid;grid-template-columns:45px 45% 20%;gap:14px;align-items:center;height:100px;border-bottom:1px solid #eee}.templates-skeleton span,.templates-skeleton p,.templates-skeleton small{height:38px;margin:0;background:linear-gradient(90deg,#f2f2f2,#fafafa,#f2f2f2);background-size:200% 100%;border-radius:8px;animation:pulse 1.4s infinite}.templates-skeleton p{height:14px}.templates-skeleton small{height:28px}.templates-state{display:grid;gap:10px;min-height:330px;color:#777;place-content:center;place-items:center}.templates-state strong{color:#333}.templates-state p{max-width:390px;margin:0;text-align:center}.templates-state button{height:34px;padding:0 15px;color:#fff;background:#171717;border:0;border-radius:8px;cursor:pointer}@keyframes pulse{to{background-position:-200% 0}}@media(max-width:800px){.templates-content{width:calc(100% - 30px);padding-top:80px}.templates-header{align-items:flex-start;flex-direction:column}.templates-header__actions{width:100%}.templates-search{flex:1}.templates-toolbar{align-items:flex-start;gap:10px}.templates-toolbar nav{flex-wrap:wrap}.template-row{grid-template-columns:45px minmax(0,1fr)}.template-row__actions{grid-column:2}.template-row__meta{flex-wrap:wrap}}
</style>

<style>
.template-action-menu{min-width:160px;padding:8px!important;border-radius:16px!important;box-shadow:0 14px 38px rgba(20,20,20,.14)!important}
.template-action-menu .n-dropdown-option{min-height:38px;border-radius:9px}
.template-action-menu .n-dropdown-option-body__prefix{display:none}
.template-action-menu .n-dropdown-option-body__label{width:100%}
.template-action-option{display:flex;width:100%;gap:10px;align-items:center}
.template-action-option__icon{display:grid;flex:0 0 18px;width:18px;height:18px;place-items:center}
.template-action-option__label{min-width:0;line-height:20px;white-space:nowrap}
.template-action-menu .template-action-option--danger,.template-action-menu .n-dropdown-option.template-action-option--danger{color:#e4202b!important}
</style>

<style scoped>
.templates-content{box-sizing:border-box;width:min(1080px,calc(100% - 56px));padding:86px 0 72px}.templates-header{align-items:center}.templates-heading h1{font-size:29px;font-weight:600;letter-spacing:-.7px}.templates-heading p{margin-top:8px;color:#787878;font-size:13px}.templates-header__actions{align-items:center}.templates-search{width:280px;height:40px;padding:0 13px;background:#fff;border-color:#dedede;border-radius:999px}.templates-upload{height:40px;padding:0 17px;border-radius:999px;transition:background .18s ease,transform .18s ease}.templates-upload:hover{background:#303030}.templates-upload:active{transform:scale(.98)}.templates-tabs{align-items:center;gap:10px;margin-top:38px;padding-bottom:20px}.templates-tabs button{padding:9px 16px;border-radius:999px}.templates-tabs button.active{color:#fff;background:#171717}.templates-tabs button.active::after{display:none}.templates-tabs>span{margin-left:8px;padding-left:18px;color:#aaa;font-size:12px;border-left:1px solid #e5e7eb}.templates-toolbar{gap:18px;padding:20px 0 24px}.templates-toolbar nav{display:flex;min-width:0;gap:8px;flex-wrap:wrap}.templates-toolbar nav button{padding:7px 14px;background:#fff;border:1px solid #e5e7eb;border-radius:999px}.templates-toolbar nav button.active{color:#fff;background:#171717;border-color:#171717}.templates-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;align-items:start}.template-card{min-width:0;overflow:hidden;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 2px 8px rgba(20,20,20,.025);outline:none;transition:border-color .2s ease,box-shadow .2s ease,transform .2s ease}.template-card--clickable{cursor:pointer}.template-card:hover,.template-card:focus-within{border-color:#d4d6d9;box-shadow:0 14px 32px rgba(20,20,20,.10);transform:translateY(-4px)}.template-card:focus-visible{box-shadow:0 0 0 3px rgba(23,23,23,.16),0 14px 32px rgba(20,20,20,.10)}.template-card__content{display:flex;box-sizing:border-box;min-height:258px;padding:20px;flex-direction:column}.template-card__tags{display:flex;min-height:24px;gap:7px;align-items:center;flex-wrap:wrap}.template-card__tags span{padding:4px 9px;color:#6d6d6d;font-size:11px;line-height:1;background:#f2f2f2;border-radius:999px}.template-card__tags span.public{color:#277157;background:#eaf6f0}.template-card h2{display:-webkit-box;min-height:48px;margin:12px 0 7px;overflow:hidden;color:#171717;font-size:17px;font-weight:610;line-height:24px;letter-spacing:-.2px;-webkit-box-orient:vertical;-webkit-line-clamp:2}.template-card__description{display:-webkit-box;min-height:42px;margin:0;overflow:hidden;color:#696969;font-size:13px;line-height:21px;-webkit-box-orient:vertical;-webkit-line-clamp:2}.template-card__details{display:flex;margin-top:14px;color:#929292;font-size:11px;line-height:17px;flex-direction:column}.template-card__footer{display:flex;min-height:39px;margin-top:auto;padding-top:15px;gap:10px;align-items:center;justify-content:space-between;border-top:1px solid #eceeef}.template-card__saves{display:flex;min-width:0;gap:5px;align-items:center;color:#8f8f8f;font-size:11px;white-space:nowrap}.template-card__actions{display:flex;gap:6px;align-items:center}.template-card__actions button{display:flex;height:34px;padding:0 12px;gap:6px;align-items:center;justify-content:center;font-size:12px;font-weight:520;border-radius:999px;cursor:pointer;transition:background .18s ease,transform .18s ease}.template-card__actions button:active{transform:scale(.97)}.template-card__actions .save{color:#262626;background:#fff;border:1px solid #d9dcdf}.template-card__actions .save:hover{background:#f4f4f4}.template-card__actions .save.saved{color:#4f655d;background:#f7faf8}.template-card__actions .save.saved :deep(.n-icon){color:#10a779}.template-card__actions .use{color:#fff;background:#171717;border:1px solid #171717}.template-card__actions .more{width:34px;padding:0;color:#555;background:#fff;border:1px solid #dedede}.template-card__actions button:disabled{cursor:not-allowed;opacity:.55}.templates-skeleton{border-top:0}.template-card--skeleton{pointer-events:none}.template-card__cover-skeleton{display:grid;min-height:238px;background:#f3f4f6;border-bottom:1px solid #e5e7eb;place-items:center}.template-card__cover-skeleton span{width:48%;aspect-ratio:210/297;background:linear-gradient(90deg,#e5e7e9,#fafafa,#e5e7e9);background-size:220% 100%;box-shadow:0 4px 12px rgba(18,24,32,.08);animation:pulse 1.4s infinite}.template-card--skeleton .template-card__content{gap:13px}.template-card--skeleton i,.template-card--skeleton strong,.template-card--skeleton p,.template-card--skeleton small{display:block;margin:0;background:linear-gradient(90deg,#ededed,#fafafa,#ededed);background-size:220% 100%;border-radius:999px;animation:pulse 1.4s infinite}.template-card--skeleton i{width:36%;height:20px}.template-card--skeleton strong{width:82%;height:19px}.template-card--skeleton p{width:100%;height:42px;border-radius:8px}.template-card--skeleton small{width:58%;height:12px}.templates-state{min-height:420px}@media(max-width:1100px){.templates-content{width:min(760px,calc(100% - 42px))}.templates-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.templates-header{align-items:flex-start;flex-direction:column}.templates-header__actions{width:100%}.templates-search{flex:1}.templates-toolbar{align-items:flex-start}}@media(max-width:720px){.templates-content{width:calc(100% - 30px);padding-top:58px}.templates-header__actions{align-items:stretch;flex-direction:column}.templates-search{box-sizing:border-box;width:100%;flex:none}.templates-upload{justify-content:center}.templates-tabs{overflow-x:auto}.templates-tabs>span{display:none}.templates-toolbar{flex-direction:column}.templates-toolbar>:deep(.app-select){align-self:flex-end}.templates-grid{grid-template-columns:minmax(0,1fr)}.template-card{width:100%;max-width:420px}.template-card__content{min-height:244px;padding:18px}}
</style>
