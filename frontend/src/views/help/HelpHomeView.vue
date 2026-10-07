<template>
  <main class="help-home">
      <header class="help-home__topbar">
        <RouterLink class="help-home__brand" to="/help">
          <span class="help-home__brand-mark"><n-icon :component="HelpCircleOutline" :size="18" /></span>
          <span>TestAgent帮助中心</span>
        </RouterLink>
      </header>

      <div class="help-home__scroll">
        <section class="help-home__hero" aria-labelledby="help-home-title">
          <p class="help-home__eyebrow">TESTAGENT GUIDE</p>
          <h1 id="help-home-title">需要什么帮助？</h1>
          <p>{{ catalog?.site.description ?? '查找 TestAgent 使用指南、工作流程与最佳实践。' }}</p>
          <HelpSearchBox v-model="query" large :loading="searching" @submit="runSearch" />
        </section>

        <div class="help-home__content">
          <HelpSearchResults
            v-if="query.trim()"
            :query="query"
            :items="searchItems"
            :loading="searching"
            :error="searchError"
            @retry="runSearch"
          />

          <template v-else-if="catalog">
            <section aria-labelledby="quick-start-title">
              <div class="help-home__section-heading">
                <div><p>从这里开始</p><h2 id="quick-start-title">快速上手 TestAgent</h2></div>
                <RouterLink to="/help/article/quick-start">查看入门指南 <n-icon :component="ArrowForwardOutline" /></RouterLink>
              </div>
              <div class="help-quick-grid">
                <HelpQuickCard
                  v-for="(article, index) in featuredArticles.slice(0, 3)"
                  :key="article.slug"
                  :article="article"
                  :index="index"
                />
              </div>
            </section>

            <section class="help-home__browse" aria-labelledby="browse-title">
              <div class="help-home__section-heading"><div><p>浏览文档</p><h2 id="browse-title">按主题查找答案</h2></div></div>
              <div class="help-section-grid">
                <article v-for="section in catalog.sections" :key="section.id" class="help-section-card">
                  <div class="help-section-card__icon"><n-icon :component="sectionIcon(section.id)" :size="19" /></div>
                  <div class="help-section-card__heading"><h3>{{ section.title }}</h3><span>{{ section.articles.length }} 篇</span></div>
                  <p>{{ section.articles[0]?.description }}</p>
                  <RouterLink v-for="article in section.articles.slice(0, 3)" :key="article.slug" :to="`/help/article/${article.slug}`">
                    {{ article.title }}<n-icon :component="ChevronForwardOutline" :size="14" />
                  </RouterLink>
                </article>
              </div>
            </section>
          </template>

          <div v-else-if="loading" class="help-home__loading" aria-label="正在加载帮助中心">
            <span v-for="index in 6" :key="index"></span>
          </div>
          <div v-else class="help-home__error">
            <h2>帮助中心暂时无法加载</h2>
            <p>{{ loadError }}</p>
            <button type="button" @click="loadCatalog">重新加载</button>
          </div>
        </div>
      </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NIcon } from 'naive-ui'
import {
  AlbumsOutline,
  ArrowForwardOutline,
  BookOutline,
  BulbOutline,
  ChevronForwardOutline,
  DocumentTextOutline,
  FolderOpenOutline,
  HelpCircleOutline,
  LibraryOutline,
  RocketOutline
} from '@vicons/ionicons5'
import HelpQuickCard from '@/components/help/HelpQuickCard.vue'
import HelpSearchBox from '@/components/help/HelpSearchBox.vue'
import HelpSearchResults from '@/components/help/HelpSearchResults.vue'
import { fetchHelpCatalog, searchHelp } from '@/api/helpApi'
import type { HelpCatalog, HelpSearchItem } from '@/types/help'

const route = useRoute()
const router = useRouter()
const catalog = ref<HelpCatalog | null>(null)
const loading = ref(true)
const loadError = ref('')
const query = ref(typeof route.query.q === 'string' ? route.query.q : '')
const searchItems = ref<HelpSearchItem[]>([])
const searching = ref(false)
const searchError = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null
let searchController: AbortController | null = null

const featuredArticles = computed(() => {
  if (!catalog.value) return []
  const featured = catalog.value.sections.flatMap((section) => section.articles.filter((article) => article.featured))
  const preferred = ['quick-start', 'prepare-requirement', 'prepare-template']
  return [...featured].sort((a, b) => {
    const ai = preferred.indexOf(a.slug)
    const bi = preferred.indexOf(b.slug)
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi)
  })
})

async function loadCatalog() {
  loading.value = true
  loadError.value = ''
  try {
    catalog.value = await fetchHelpCatalog({ force: Boolean(catalog.value) })
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '请稍后重试。'
  } finally {
    loading.value = false
  }
}

async function runSearch() {
  const value = query.value.trim()
  void router.replace({ path: '/help', query: value ? { q: value } : {} })
  searchController?.abort()
  searchError.value = ''
  if (!value) {
    searchItems.value = []
    searching.value = false
    return
  }
  const controller = new AbortController()
  searchController = controller
  searching.value = true
  try {
    searchItems.value = (await searchHelp(value, 20, controller.signal)).items
  } catch (error) {
    if ((error as Error).name !== 'AbortError') searchError.value = error instanceof Error ? error.message : '搜索失败，请稍后重试。'
  } finally {
    if (searchController === controller) searching.value = false
  }
}

function scheduleSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(runSearch, 250)
}

function sectionIcon(id: string) {
  return ({
    'getting-started': RocketOutline,
    concepts: BulbOutline,
    'test-plan': DocumentTextOutline,
    templates: AlbumsOutline,
    projects: FolderOpenOutline,
    library: LibraryOutline,
    knowledge: BookOutline,
    'best-practices': BulbOutline,
    faq: HelpCircleOutline
  } as Record<string, typeof RocketOutline>)[id] ?? DocumentTextOutline
}

function focusShortcut(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null
  if (event.key !== '/' || target?.matches('input, textarea, [contenteditable="true"]')) return
  event.preventDefault()
  document.querySelector<HTMLInputElement>('.help-search input')?.focus()
}

watch(query, scheduleSearch)
onMounted(() => {
  void loadCatalog()
  if (query.value) void runSearch()
  window.addEventListener('keydown', focusShortcut)
})
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  searchController?.abort()
  window.removeEventListener('keydown', focusShortcut)
})
</script>

<style scoped>
.help-home { display: flex; width: 100%; min-width: 0; height: 100dvh; min-height: 100dvh; flex-direction: column; overflow: hidden; background: #f7f7f6; }
.help-home__topbar { display: flex; flex: 0 0 65px; align-items: center; justify-content: space-between; padding: 0 34px; background: rgba(255,255,255,.92); border-bottom: 1px solid #e5e7eb; }
.help-home__brand { display: flex; align-items: center; gap: 10px; color: #17191d; font-size: 15px; font-weight: 650; }
.help-home__brand-mark { display: grid; width: 30px; height: 30px; color: #fff; background: #191919; border-radius: 8px; place-items: center; }
.help-home__section-heading > a { display: flex; align-items: center; gap: 5px; color: #656b74; font-size: 13px; }
.help-home__section-heading > a:hover { color: #111; }
.help-home__scroll { min-height: 0; flex: 1; overflow-y: auto; }
.help-home__hero { padding: 78px 24px 71px; text-align: center; background: #f1f1ef; border-bottom: 1px solid #e3e3e0; }
.help-home__eyebrow { margin: 0 0 13px !important; color: #7b8088 !important; font-size: 11px !important; font-weight: 700; letter-spacing: .14em; }
.help-home__hero h1 { margin: 0; color: #111317; font-size: clamp(32px, 4vw, 46px); font-weight: 650; line-height: 1.18; letter-spacing: -.035em; }
.help-home__hero > p { margin: 15px 0 27px; color: #6c727b; font-size: 15px; }
.help-home__hero :deep(.help-search) { margin: 0 auto; text-align: left; }
.help-home__content { width: min(1120px, calc(100% - 48px)); margin: 0 auto; padding: 58px 0 80px; }
.help-home__section-heading { display: flex; align-items: end; justify-content: space-between; margin-bottom: 22px; }
.help-home__section-heading p { margin: 0 0 5px; color: #969ba3; font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.help-home__section-heading h2 { margin: 0; color: #17191d; font-size: 24px; font-weight: 650; letter-spacing: -.02em; }
.help-quick-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.help-home__browse { margin-top: 66px; }
.help-section-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 15px; }
.help-section-card { padding: 22px; background: #fff; border: 1px solid #e3e5e8; border-radius: 13px; }
.help-section-card__icon { display: grid; width: 34px; height: 34px; margin-bottom: 15px; color: #474c54; background: #f0f1f2; border-radius: 9px; place-items: center; }
.help-section-card__heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.help-section-card h3 { margin: 0; color: #1b1e23; font-size: 16px; }
.help-section-card__heading span { color: #a0a4aa; font-size: 11px; }
.help-section-card > p { min-height: 42px; margin: 7px 0 13px; color: #7a8089; font-size: 12px; line-height: 20px; }
.help-section-card > a { display: flex; align-items: center; justify-content: space-between; padding: 6px 0; color: #575d66; font-size: 13px; border-top: 1px solid #f0f1f2; }
.help-section-card > a:hover { color: #111; }
.help-home__loading { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; }
.help-home__loading span { height: 170px; background: #e9eaeb; border-radius: 13px; animation: help-pulse 1.2s ease-in-out infinite alternate; }
.help-home__error { padding: 60px 24px; text-align: center; background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; }
.help-home__error h2 { margin: 0 0 7px; color: #23262b; font-size: 19px; }
.help-home__error p { margin: 0; color: #777d86; }
.help-home__error button { margin-top: 18px; padding: 9px 18px; color: #fff; cursor: pointer; background: #111; border: 0; border-radius: 8px; }
@keyframes help-pulse { to { opacity: .5; } }
@media (max-width: 1050px) { .help-quick-grid, .help-section-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 680px) {
  .help-home__topbar { padding: 0 18px; }
  .help-home__hero { padding: 55px 18px; }
  .help-home__content { width: calc(100% - 30px); padding-top: 40px; }
  .help-quick-grid, .help-section-grid, .help-home__loading { grid-template-columns: 1fr; }
  .help-home__section-heading > a { display: none; }
}
</style>
