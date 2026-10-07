import { apiGet } from './request'
import type {
  HelpArticle,
  HelpArticleSummary,
  HelpCatalog,
  HelpSearchItem,
  HelpSearchResult,
  HelpSection
} from '@/types/help'

interface RawArticleSummary extends Omit<HelpArticleSummary, 'readingTimeMinutes' | 'updatedAt'> {
  reading_time_minutes: number
  updated_at: string | null
}

interface RawSection extends Omit<HelpSection, 'articles'> {
  articles: RawArticleSummary[]
}

interface RawCatalog extends Omit<HelpCatalog, 'sections'> {
  sections: RawSection[]
}

interface RawArticle extends Omit<HelpArticle, 'readingTimeMinutes' | 'updatedAt'> {
  reading_time_minutes: number
  updated_at: string | null
}

interface RawSearchItem extends Omit<HelpSearchItem, 'sectionId' | 'sectionTitle'> {
  section_id: string
  section_title: string
}

interface RawSearchResult {
  query: string
  items: RawSearchItem[]
}

let catalogCache: HelpCatalog | null = null
const articleCache = new Map<string, HelpArticle>()

function mapSummary(raw: RawArticleSummary): HelpArticleSummary {
  return {
    slug: raw.slug,
    title: raw.title,
    description: raw.description,
    order: raw.order,
    keywords: raw.keywords,
    featured: raw.featured,
    readingTimeMinutes: raw.reading_time_minutes,
    updatedAt: raw.updated_at
  }
}

export async function fetchHelpCatalog(options: { force?: boolean; signal?: AbortSignal } = {}): Promise<HelpCatalog> {
  if (catalogCache && !options.force) return catalogCache
  const raw = await apiGet<RawCatalog>('/api/help', { signal: options.signal })
  catalogCache = {
    site: raw.site,
    sections: raw.sections.map((section) => ({
      id: section.id,
      title: section.title,
      order: section.order,
      articles: section.articles.map(mapSummary)
    }))
  }
  return catalogCache
}

export async function fetchHelpArticle(
  slug: string,
  options: { force?: boolean; signal?: AbortSignal } = {}
): Promise<HelpArticle> {
  const cached = articleCache.get(slug)
  if (cached && !options.force) return cached
  const raw = await apiGet<RawArticle>(`/api/help/articles/${encodeURIComponent(slug)}`, { signal: options.signal })
  const article: HelpArticle = {
    ...raw,
    readingTimeMinutes: raw.reading_time_minutes,
    updatedAt: raw.updated_at
  }
  delete (article as Partial<RawArticle>).reading_time_minutes
  delete (article as Partial<RawArticle>).updated_at
  articleCache.set(slug, article)
  return article
}

export async function searchHelp(query: string, limit = 20, signal?: AbortSignal): Promise<HelpSearchResult> {
  const params = new URLSearchParams({ q: query, limit: String(Math.min(50, Math.max(1, limit))) })
  const raw = await apiGet<RawSearchResult>(`/api/help/search?${params.toString()}`, { signal })
  return {
    query: raw.query,
    items: raw.items.map((item) => ({
      slug: item.slug,
      title: item.title,
      sectionId: item.section_id,
      sectionTitle: item.section_title,
      description: item.description,
      snippet: item.snippet,
      score: item.score
    }))
  }
}

export function clearHelpApiCache() {
  catalogCache = null
  articleCache.clear()
}
