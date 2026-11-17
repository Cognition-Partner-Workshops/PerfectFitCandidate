import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TaskProvider } from './TaskProvider'
import { mergeJobs, useTasks } from '../lib/tasks'
import type { JobItem } from '../api/types'
import { ApiError } from '../api/client'

const job: JobItem = { jobId: 10, kind: 'cv_sync', status: 'running', progress: 0.3, label: 'CV Sync for 3 profiles', message: null, link: '/#/bench', createdAt: '2026-01-01T00:00:00Z', startedAt: null, finishedAt: null }
const flush = async () => { await act(async () => { await Promise.resolve() }) }

function mount(jobs: JobItem[] = []) {
  const client = { listJobs: vi.fn().mockResolvedValue(jobs) }
  const hook = renderHook(useTasks, { wrapper: ({ children }: { children: ReactNode }) => <TaskProvider client={client}>{children}</TaskProvider> })
  return { ...hook, client }
}

describe('task store', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('polls at 15 seconds idle and 2 seconds while active, pauses hidden, resumes immediately', async () => {
    const { client, result } = mount()
    await flush()
    expect(client.listJobs).toHaveBeenCalledTimes(1)
    await act(() => vi.advanceTimersByTimeAsync(14999))
    expect(client.listJobs).toHaveBeenCalledTimes(1)
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(client.listJobs).toHaveBeenCalledTimes(2)
    act(() => result.current.addJob(10, 'CV sync'))
    await flush()
    const calls = client.listJobs.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(1999))
    expect(client.listJobs).toHaveBeenCalledTimes(calls)
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(client.listJobs).toHaveBeenCalledTimes(calls + 1)
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await act(() => vi.advanceTimersByTimeAsync(20000))
    expect(client.listJobs).toHaveBeenCalledTimes(calls + 1)
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await flush()
    expect(client.listJobs).toHaveBeenCalledTimes(calls + 2)
  })

  it('deduplicates placeholders, preserves local tasks and terminal times, and respects dismissals', () => {
    const tasks = [{ id: 'job-10', jobId: 10, label: 'Placeholder', status: 'queued' as const }, { id: 'local-1', label: 'DL upload', status: 'running' as const }]
    const merged = mergeJobs(tasks, [job, job], new Set(), 100)
    expect(merged).toHaveLength(2)
    expect(merged[0]).toMatchObject({ label: job.label, status: 'running', progress: 0.3 })
    const done = mergeJobs(merged, [{ ...job, status: 'completed' }], new Set(), 200)
    expect(mergeJobs(done, [{ ...job, status: 'completed' }], new Set(), 300)[0].endedAt).toBe(200)
    expect(mergeJobs(done, [job], new Set(['job-10']), 300)[0].hidden).toBe(true)
  })

  it('tracks client success, auto-dismisses once after ten seconds and never restores dismissed jobs', async () => {
    const { result, client } = mount([job])
    await flush()
    let resolve!: (value: number) => void
    const promise = new Promise<number>((done) => { resolve = done })
    let tracked!: Promise<number>
    act(() => { tracked = result.current.trackTask('DL upload: demand.xlsx', promise) })
    expect(result.current.tasks.find((task) => task.id.startsWith('local'))?.status).toBe('queued')
    await act(() => vi.advanceTimersByTimeAsync(0))
    expect(result.current.tasks.find((task) => task.id.startsWith('local'))?.status).toBe('running')
    await act(async () => { resolve(42); expect(await tracked).toBe(42) })
    expect(result.current.tasks.find((task) => task.id.startsWith('local'))?.status).toBe('completed')
    await act(() => vi.advanceTimersByTimeAsync(9999))
    expect(result.current.tasks).toHaveLength(2)
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(result.current.tasks).toHaveLength(1)
    act(() => result.current.dismiss('job-10'))
    await flush()
    await act(() => vi.advanceTimersByTimeAsync(15000))
    expect(client.listJobs.mock.calls.length).toBeGreaterThan(2)
    expect(result.current.tasks).toEqual([])
    expect(result.current.busy).toBe(true)
  })

  it('keeps failures until closed, supplies a safe reason and rethrows to the caller', async () => {
    const { result } = mount()
    await flush()
    const error = new Error('private@example.com /home/private/cv.docx')
    await act(async () => { await expect(result.current.trackTask('CT commit', Promise.reject(error))).rejects.toBe(error) })
    expect(result.current.tasks[0]).toMatchObject({ status: 'failed', reason: 'Please retry or contact an administrator.' })
    await act(() => vi.advanceTimersByTimeAsync(20000))
    expect(result.current.tasks).toHaveLength(1)
    await act(async () => { await result.current.trackTask('DL upload', Promise.reject(new ApiError({ status: 422, title: 'raw sensitive text' }))).catch(() => {}) })
    expect(result.current.tasks[1].reason).toBe('Check the input and retry.')
  })

  it('retains tasks after poll failures and retries', async () => {
    const { result, client } = mount([job])
    await flush()
    client.listJobs.mockRejectedValueOnce(new Error('offline'))
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(result.current.tasks[0].jobId).toBe(10)
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(client.listJobs.mock.calls.length).toBeGreaterThan(2)
  })

  it('marks a resolved partial failure as failed without changing the returned value', async () => {
    const { result } = mount()
    await flush()
    const response = { failed: [{ detail: 'private@example.com /home/private/file' }] }
    await act(async () => {
      expect(await result.current.trackTask('Analyze all new (5)', Promise.resolve(response), {
        failureReason: (value) => value.failed.length ? `${value.failed.length} of 5 failed` : undefined,
      })).toBe(response)
    })
    expect(result.current.tasks[0]).toMatchObject({ status: 'failed', reason: '1 of 5 failed' })
    await act(() => vi.advanceTimersByTimeAsync(10000))
    expect(result.current.tasks).toHaveLength(1)
    await act(async () => {
      await result.current.trackTask('Analyze all new (1)', Promise.resolve({ failed: [] }), {
        failureReason: (value) => value.failed.length ? '1 of 1 failed' : undefined,
      })
    })
    expect(result.current.tasks[1].status).toBe('completed')
  })

  it('returns to the idle interval when jobs finish and does not restart completion expiry', async () => {
    const { client, result } = mount([job])
    await flush()
    client.listJobs.mockResolvedValue([{ ...job, status: 'completed' }])
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(result.current.tasks[0].status).toBe('completed')
    const calls = client.listJobs.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(9999))
    expect(client.listJobs).toHaveBeenCalledTimes(calls)
    expect(result.current.tasks).toHaveLength(1)
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(result.current.tasks).toEqual([])
    await act(() => vi.advanceTimersByTimeAsync(5000))
    expect(client.listJobs).toHaveBeenCalledTimes(calls + 1)
    expect(result.current.tasks).toEqual([])
  })
})
