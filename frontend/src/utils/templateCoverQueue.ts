const MAX_CONCURRENT_TEMPLATE_COVERS = 2

let activeTemplateCoverLoads = 0
const templateCoverWaiters: Array<() => void> = []

/**
 * Keeps expensive first-page preview generation bounded across every card on
 * the current page, rather than creating an independent queue per card.
 */
export async function withTemplateCoverSlot<T>(job: () => Promise<T>): Promise<T> {
  if (activeTemplateCoverLoads >= MAX_CONCURRENT_TEMPLATE_COVERS) {
    await new Promise<void>((resolve) => templateCoverWaiters.push(resolve))
  }

  activeTemplateCoverLoads += 1
  try {
    return await job()
  } finally {
    activeTemplateCoverLoads -= 1
    templateCoverWaiters.shift()?.()
  }
}
