export interface HelpSite {
  title: string
  description: string
}

export interface HelpArticleSummary {
  slug: string
  title: string
  description: string
  order: number
  keywords: string[]
  featured: boolean
  readingTimeMinutes: number
  updatedAt: string | null
}

export interface HelpSection {
  id: string
  title: string
  order: number
  articles: HelpArticleSummary[]
}

export interface HelpCatalog {
  site: HelpSite
  sections: HelpSection[]
}

export interface HelpHeading {
  level: 2 | 3
  text: string
  anchor: string
}

export interface HelpAdjacentArticle {
  slug: string
  title: string
}

export interface HelpArticle {
  slug: string
  title: string
  description: string
  section: { id: string; title: string }
  markdown: string
  headings: HelpHeading[]
  readingTimeMinutes: number
  updatedAt: string | null
  previous: HelpAdjacentArticle | null
  next: HelpAdjacentArticle | null
}

export interface HelpSearchItem {
  slug: string
  title: string
  sectionId: string
  sectionTitle: string
  description: string
  snippet: string
  score: number
}

export interface HelpSearchResult {
  query: string
  items: HelpSearchItem[]
}
