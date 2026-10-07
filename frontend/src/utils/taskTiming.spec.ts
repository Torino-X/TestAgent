import { describe, expect, it } from 'vitest'
import {
  deriveTaskElapsedSource,
  formatTaskElapsed,
  formatToolElapsed,
  resolveTaskElapsedMs,
  shouldTickTaskElapsed
} from './taskTiming'

describe('task timing', () => {
  it('can derive a running task clock from the earliest known run event', () => {
    const source = deriveTaskElapsedSource(
      { status: 'running' },
      'running',
      [
        undefined,
        '2026-07-24T00:00:05.000Z',
        '2026-07-24T00:00:00.000Z',
      ],
    )

    expect(
      formatTaskElapsed(source, Date.parse('2026-07-24T00:01:05.000Z')),
    ).toBe('1m 05s')
  })

  it('does not infer a task duration from unrelated message timestamps', () => {
    expect(formatTaskElapsed({ status: 'running' }, Date.now())).toBe('—')
  })

  it('uses the backend task clock while a task is still running', () => {
    const now = Date.parse('2026-07-24T00:00:05.000Z')
    expect(
      formatTaskElapsed(
        { status: 'running', startedAt: '2026-07-24T00:00:00.000Z' },
        now,
      ),
    ).toBe('0m 05s')
  })

  it('prefers the backend supplied duration for completed tasks', () => {
    expect(formatTaskElapsed({ status: 'completed', durationMs: 65_000 }, Date.now())).toBe('1m 05s')
  })

  it('formats a completed task with the authoritative 169 second task duration', () => {
    expect(
      formatTaskElapsed({
        status: 'completed',
        startedAt: '2026-08-01T04:58:45+00:00',
        completedAt: '2026-08-01T05:01:34+00:00',
        durationMs: 169000
      }, Date.parse('2026-08-01T05:10:00+00:00'))
    ).toBe('2m 49s')
  })

  it('uses completedAt minus startedAt when terminal durationMs is missing', () => {
    expect(
      formatTaskElapsed({
        status: 'completed',
        startedAt: '2026-08-01T04:58:45+00:00',
        completedAt: '2026-08-01T05:01:34+00:00'
      }, Date.parse('2026-08-01T05:10:00+00:00'))
    ).toBe('2m 49s')
  })

  it('derives a terminal completedAt from the latest live event when task detail lags', () => {
    const source = deriveTaskElapsedSource(
      {
        status: 'completed',
        startedAt: '2026-08-01T04:58:45+00:00',
      },
      'completed',
      [],
      [
        '2026-08-01T04:58:46+00:00',
        '2026-08-01T04:59:30+00:00',
      ],
    )

    expect(
      formatTaskElapsed(source, Date.parse('2026-08-01T05:10:00+00:00'))
    ).toBe('0m 45s')
  })

  it('keeps waiting_user_confirm on the task wall clock', () => {
    const now = Date.parse('2026-08-01T04:59:15+00:00')
    expect(
      resolveTaskElapsedMs({
        status: 'waiting_user_confirm',
        startedAt: '2026-08-01T04:58:45+00:00',
        nowMs: now
      })
    ).toBe(30_000)
  })

  it('continues ticking while a task is waiting for user confirmation', () => {
    expect(shouldTickTaskElapsed('waiting_user_confirm')).toBe(true)
    expect(shouldTickTaskElapsed('waiting')).toBe(true)
    expect(shouldTickTaskElapsed('completed')).toBe(false)
  })

  it('does not let run timing override task lifecycle timing', () => {
    expect(
      formatTaskElapsed({
        status: 'completed',
        startedAt: '2026-08-01T04:58:45+00:00',
        completedAt: '2026-08-01T05:01:34+00:00',
        run: {
          startedAt: '2026-08-01T05:01:00+00:00',
          finishedAt: '2026-08-01T05:01:34+00:00',
          durationMs: 34000
        }
      }, Date.parse('2026-08-01T05:10:00+00:00'))
    ).toBe('2m 49s')
  })

  it('returns null elapsed when startedAt is missing instead of showing fake zero', () => {
    expect(
      resolveTaskElapsedMs({
        status: 'running',
        nowMs: Date.parse('2026-08-01T05:10:00+00:00')
      })
    ).toBeNull()
  })

  it('shows minimum 3s for running tool without start time', () => {
    expect(formatToolElapsed({ status: 'running' }, Date.now())).toBe('3s')
  })

  it('shows minimum 3s for tool with very short duration', () => {
    expect(
      formatToolElapsed({
        status: 'success',
        startedAt: '2026-07-24T00:00:00.000Z',
        finishedAt: '2026-07-24T00:00:00.500Z',
      }, Date.now()),
    ).toBe('3s')
  })

  it('shows integer seconds for tool duration', () => {
    expect(
      formatToolElapsed({
        status: 'success',
        startedAt: '2026-07-24T00:00:00.000Z',
        finishedAt: '2026-07-24T00:00:05.250Z',
      }, Date.now()),
    ).toBe('5s')
  })

  it('parses explicit duration and applies minimum 3s rule', () => {
    expect(formatToolElapsed({ duration: '0.4s' }, Date.now())).toBe('3s')
  })

  it('parses explicit duration and keeps integer seconds', () => {
    expect(formatToolElapsed({ duration: '5.2s' }, Date.now())).toBe('5s')
  })
})
