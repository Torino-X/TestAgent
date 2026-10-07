import { describe, expect, it, vi } from 'vitest'
import { withTemplateCoverSlot } from './templateCoverQueue'

describe('template cover queue', () => {
  it('runs no more than two cover jobs at once', async () => {
    let active = 0
    let peak = 0
    const releases: Array<() => void> = []

    const run = () => withTemplateCoverSlot(() => new Promise<void>((resolve) => {
      active += 1
      peak = Math.max(peak, active)
      releases.push(() => {
        active -= 1
        resolve()
      })
    }))

    const jobs = [run(), run(), run()]
    await vi.waitFor(() => expect(releases).toHaveLength(2))
    releases.shift()?.()
    await vi.waitFor(() => expect(releases).toHaveLength(2))
    releases.shift()?.()
    releases.shift()?.()

    await Promise.all(jobs)
    expect(peak).toBe(2)
  })
})
