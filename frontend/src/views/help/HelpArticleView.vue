<template>
  <main class="help-article-page">
      <header class="help-article-page__topbar">
        <div class="help-article-page__topbar-left">
          <button class="help-article-page__menu" type="button" aria-label="打开文档目录" @click="drawerOpen = true">
            <n-icon :component="MenuOutline" :size="21" />
          </button>
          <RouterLink class="help-article-page__brand" to="/help">
            <span><n-icon :component="HelpCircleOutline" :size="17" /></span>TestAgent帮助中心
          </RouterLink>
        </div>
        <HelpSearchBox v-model="searchQuery" placeholder="搜索帮助文档" @submit="submitSearch" />
      </header>

      <div class="help-article-page__body">
        <aside class="help-article-page__nav">
          <HelpCategoryNav :sections="catalog?.sections ?? []" :active-slug="slug" />
        </aside>

        <div ref="scrollRoot" class="help-article-page__scroll">
          <div v-if="loading" class="help-article-skeleton" aria-label="正在加载文章">
            <span class="wide"></span><span></span><span></span><span class="short"></span>
          </div>

          <section v-else-if="loadError || !article" class="help-article-error">
            <n-icon :component="DocumentTextOutline" :size="36" />
            <h1>{{ notFound ? '没有找到这篇文章' : '文章暂时无法加载' }}</h1>
            <p>{{ loadError || '文章可能已移动或删除。' }}</p>
            <div><RouterLink to="/help">返回帮助首页</RouterLink><button type="button" @click="loadArticle">重新加载</button></div>
          </section>

          <div v-else class="help-article-layout">
            <article class="help-article">
              <nav class="help-breadcrumb" aria-label="面包屑">
                <RouterLink to="/help">帮助中心</RouterLink><n-icon :component="ChevronForwardOutline" /><span>{{ article.section.title }}</span>
              </nav>
              <header class="help-article__header">
                <p class="help-article__section">{{ article.section.title }}</p>
                <h1>{{ article.title }}</h1>
                <p class="help-article__description">{{ article.description }}</p>
                <div class="help-article__meta">
                  <span><n-icon :component="TimeOutline" />约 {{ article.readingTimeMinutes }} 分钟阅读</span>
                  <span v-if="article.updatedAt"><n-icon :component="CalendarClearOutline" />更新于 {{ formatDate(article.updatedAt) }}</span>
                </div>
              </header>
              <HelpMarkdownRenderer :source="article.markdown" />
              <HelpPrevNext :previous="article.previous" :next="article.next" />
            </article>

            <aside class="help-article-page__toc">
              <HelpTableOfContents :headings="article.headings" :active-anchor="activeAnchor" @select="activeAnchor = $event" />
            </aside>
          </div>
        </div>
      </div>

      <Teleport to="body">
        <div v-if="drawerOpen" class="help-drawer" role="dialog" aria-modal="true" aria-label="帮助文档目录" @click.self="drawerOpen = false">
          <aside>
            <div class="help-drawer__header"><strong>文档目录</strong><button type="button" aria-label="关闭目录" @click="drawerOpen = false"><n-icon :component="CloseOutline" /></button></div>
            <HelpCategoryNav :sections="catalog?.sections ?? []" :active-slug="slug" @navigate="drawerOpen = false" />
          </aside>
        </div>
      </Teleport>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NIcon } from 'naive-ui'
import {
  CalendarClearOutline,
  ChevronForwardOutline,
  CloseOutline,
  DocumentTextOutline,
  HelpCircleOutline,
  MenuOutline,
  TimeOutline
} from '@vicons/ionicons5'
import HelpCategoryNav from '@/components/help/HelpCategoryNav.vue'
import HelpMarkdownRenderer from '@/components/help/HelpMarkdownRenderer.vue'
import HelpPrevNext from '@/components/help/HelpPrevNext.vue'
import HelpSearchBox from '@/components/help/HelpSearchBox.vue'
import HelpTableOfContents from '@/components/help/HelpTableOfContents.vue'
import { ApiRequestError } from '@/api/request'
import { fetchHelpArticle, fetchHelpCatalog } from '@/api/helpApi'
import type { HelpArticle, HelpCatalog } from '@/types/help'

const route = useRoute()
const router = useRouter()
const catalog = ref<HelpCatalog | null>(null)
const article = ref<HelpArticle | null>(null)
const loading = ref(true)
const loadError = ref('')
const notFound = ref(false)
const searchQuery = ref('')
const drawerOpen = ref(false)
const activeAnchor = ref('')
const scrollRoot = ref<HTMLElement | null>(null)
let observer: IntersectionObserver | null = null
let requestController: AbortController | null = null

const slug = computed(() => String(route.params.slug ?? ''))

function submitSearch() {
  const query = searchQuery.value.trim()
  void router.push({ path: '/help', query: query ? { q: query } : {} })
}

function formatDate(value: string) {
  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' }).format(date)
}

function setupHeadingObserver() {
  observer?.disconnect()
  if (!article.value || typeof IntersectionObserver === 'undefined') return
  const headings = article.value.headings
    .map((heading) => document.getElementById(heading.anchor))
    .filter((heading): heading is HTMLElement => Boolean(heading))
  if (!headings.length) return
  observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
    if (visible[0]?.target.id) activeAnchor.value = visible[0].target.id
  }, { root: scrollRoot.value, rootMargin: '-12% 0px -72% 0px', threshold: [0, 1] })
  headings.forEach((heading) => observer?.observe(heading))
}

async function restoreHash() {
  await nextTick()
  setupHeadingObserver()
  if (route.hash) document.getElementById(decodeURIComponent(route.hash.slice(1)))?.scrollIntoView({ block: 'start' })
  else scrollRoot.value?.scrollTo({ top: 0 })
}

async function loadArticle() {
  requestController?.abort()
  const controller = new AbortController()
  requestController = controller
  loading.value = true
  loadError.value = ''
  notFound.value = false
  observer?.disconnect()
  try {
    const [nextCatalog, nextArticle] = await Promise.all([
      fetchHelpCatalog({ signal: controller.signal }),
      fetchHelpArticle(slug.value, { signal: controller.signal })
    ])
    if (requestController !== controller) return
    catalog.value = nextCatalog
    article.value = nextArticle
    document.title = `${nextArticle.title} · TestAgent 帮助中心`
    loading.value = false
    await restoreHash()
  } catch (error) {
    if ((error as Error).name === 'AbortError') return
    article.value = null
    notFound.value = error instanceof ApiRequestError && error.status === 404
    loadError.value = error instanceof Error ? error.message : '请稍后重试。'
  } finally {
    if (requestController === controller) loading.value = false
  }
}

watch(slug, () => { void loadArticle() }, { immediate: true })
onBeforeUnmount(() => {
  requestController?.abort()
  observer?.disconnect()
  document.title = 'TestAgent'
})
</script>

<style scoped>
.help-article-page { display: flex; width: 100%; min-width: 0; height: 100dvh; min-height: 100dvh; flex-direction: column; overflow: hidden; background: #fff; }
.help-article-page__topbar { display: grid; flex: 0 0 65px; grid-template-columns: 1fr minmax(260px, 420px) 1fr; align-items: center; padding: 0 28px; gap: 20px; background: rgba(255,255,255,.96); border-bottom: 1px solid #e5e7eb; }
.help-article-page__topbar-left { display: flex; align-items: center; gap: 8px; }
.help-article-page__brand { display: flex; align-items: center; gap: 9px; color: #202328; font-size: 14px; font-weight: 650; }
.help-article-page__brand > span { display: grid; width: 29px; height: 29px; color: #fff; background: #171717; border-radius: 8px; place-items: center; }
.help-article-page__menu { display: none; padding: 6px; color: #5f6670; cursor: pointer; background: transparent; border: 0; }
.help-article-page__body { display: flex; min-height: 0; flex: 1; }
.help-article-page__nav { flex: 0 0 232px; overflow-y: auto; background: #fafafa; border-right: 1px solid #e6e7e9; }
.help-article-page__scroll { min-width: 0; flex: 1; overflow-y: auto; scroll-behavior: smooth; }
.help-article-layout { display: grid; grid-template-columns: minmax(0, 760px) 190px; justify-content: center; padding: 48px 52px 100px; gap: 74px; }
.help-article { min-width: 0; }
.help-article-page__toc { position: sticky; top: 42px; align-self: start; }
.help-breadcrumb { display: flex; align-items: center; margin-bottom: 28px; gap: 5px; color: #92979e; font-size: 12px; }
.help-breadcrumb a:hover { color: #333; }
.help-breadcrumb .n-icon { font-size: 13px; }
.help-article__header { padding-bottom: 34px; border-bottom: 1px solid #e8e9eb; }
.help-article__section { margin: 0 0 10px; color: #7b818a; font-size: 12px; font-weight: 650; }
.help-article__header h1 { margin: 0; color: #131519; font-size: clamp(30px, 3.1vw, 40px); font-weight: 680; line-height: 1.2; letter-spacing: -.035em; }
.help-article__description { margin: 15px 0 18px; color: #686f78; font-size: 16px; line-height: 27px; }
.help-article__meta { display: flex; flex-wrap: wrap; gap: 18px; color: #979ca4; font-size: 12px; }
.help-article__meta span { display: flex; align-items: center; gap: 5px; }
.help-article__meta .n-icon { font-size: 15px; }
.help-article :deep(.help-markdown) { padding-top: 10px; }
.help-article-skeleton { width: min(760px, calc(100% - 80px)); margin: 80px auto; }
.help-article-skeleton span { display: block; width: 100%; height: 17px; margin-bottom: 18px; background: #eceeef; border-radius: 6px; animation: help-article-pulse 1.2s ease-in-out infinite alternate; }
.help-article-skeleton .wide { height: 43px; margin-bottom: 34px; }
.help-article-skeleton .short { width: 64%; }
.help-article-error { width: min(620px, calc(100% - 40px)); margin: 100px auto; color: #8b9199; text-align: center; }
.help-article-error h1 { margin: 16px 0 7px; color: #22262b; font-size: 24px; }
.help-article-error p { margin: 0; }
.help-article-error div { display: flex; justify-content: center; margin-top: 22px; gap: 10px; }
.help-article-error a, .help-article-error button { padding: 9px 16px; cursor: pointer; background: #fff; border: 1px solid #d9dce0; border-radius: 8px; }
.help-article-error button { color: #fff; background: #111; border-color: #111; }
.help-drawer { position: fixed; inset: 0; z-index: 1200; display: none; background: rgba(17,24,39,.28); }
.help-drawer > aside { width: min(320px, 86vw); height: 100%; overflow-y: auto; background: #fff; box-shadow: 12px 0 34px rgba(17,24,39,.12); }
.help-drawer__header { display: flex; align-items: center; justify-content: space-between; height: 60px; padding: 0 20px; border-bottom: 1px solid #e5e7eb; }
.help-drawer__header button { display: grid; padding: 6px; cursor: pointer; background: transparent; border: 0; place-items: center; }
@keyframes help-article-pulse { to { opacity: .45; } }
@media (max-width: 1280px) {
  .help-article-layout { grid-template-columns: minmax(0, 760px); }
  .help-article-page__toc { display: none; }
}
@media (max-width: 1100px) {
  .help-article-page__nav { display: none; }
  .help-article-page__menu { display: grid; place-items: center; }
  .help-drawer { display: block; }
}
@media (max-width: 760px) {
  .help-article-page__topbar { grid-template-columns: 1fr auto; padding: 0 15px; }
  .help-article-page__topbar :deep(.help-search) { display: none; }
  .help-article-layout { padding: 32px 21px 70px; }
  .help-article__description { font-size: 15px; }
}
</style>
