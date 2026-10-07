import { describe, expect, it } from 'vitest'
import appSelectSource from './AppSelect.vue?raw'
import agentRunSource from '@/components/cards/AgentRunCard.vue?raw'
import sectionConfirmSource from '@/components/cards/SectionConfirmCard.vue?raw'
import importDrawerSource from '@/components/knowledge/dialogs/KnowledgeDocImportDrawer.vue?raw'
import chunkManagerSource from '@/components/knowledge/KnowledgeChunkManager.vue?raw'
import documentManagerSource from '@/components/knowledge/KnowledgeDocumentManager.vue?raw'
import retrieveTesterSource from '@/components/knowledge/KnowledgeRetrieveTester.vue?raw'
import knowledgeSettingsSource from '@/components/knowledge/KnowledgeSettings.vue?raw'
import uploadDialogSource from '@/components/templates/TemplateUploadDialog.vue?raw'
import templateMarketSource from '@/views/TemplateMarketView.vue?raw'

const migratedSources = [
  agentRunSource,
  sectionConfirmSource,
  importDrawerSource,
  chunkManagerSource,
  documentManagerSource,
  retrieveTesterSource,
  knowledgeSettingsSource,
  uploadDialogSource,
  templateMarketSource
]

describe('AppSelect visual contract', () => {
  it('renders a custom selection menu with selected state and checkmark', () => {
    expect(appSelectSource).toContain("class: 'app-select-menu'")
    expect(appSelectSource).toContain('app-select-menu__option--selected')
    expect(appSelectSource).toContain('CheckmarkOutline')
    expect(appSelectSource).toContain(':aria-expanded="menuOpen"')
    expect(appSelectSource).toContain(':disabled="disabled"')
  })

  it('lets the menu grow to fit complete option labels without truncation', () => {
    expect(appSelectSource).toContain('width: max-content;')
    expect(appSelectSource).toContain('max-width: none;')
    expect(appSelectSource).toContain("overflow: 'visible'")
    expect(appSelectSource).toContain("textOverflow: 'clip'")
  })

  it('removes browser-native selects from every migrated product surface', () => {
    for (const source of migratedSources) {
      expect(source).not.toMatch(/<select(?:\s|>)/i)
      expect(source).not.toMatch(/<n-select(?:\s|>)/i)
    }
    expect(migratedSources.filter((source) => source.includes('<AppSelect')).length).toBe(migratedSources.length)
  })
})
