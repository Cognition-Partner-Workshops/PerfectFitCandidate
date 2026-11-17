import { afterEach, expect, it, vi } from 'vitest'
import { HttpApiClient } from './http'
import { toJob } from './mappers'
import { ADMIN, mock, seed, signIn } from '../test/utils'
import type { JobItem } from './types'

afterEach(() => vi.unstubAllGlobals())

it('requests jobs with default and custom limits, maps the camelCase contract and empty results', async () => {
  const job: JobItem = { jobId: 12, kind: 'embed', label: 'Re-indexing CVs', status: 'completed', progress: 1, message: null, link: null, createdAt: '2026-01-01', startedAt: null, finishedAt: null }
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify([job]))).mockResolvedValueOnce(new Response('[]'))
  vi.stubGlobal('fetch', fetch)
  const api = new HttpApiClient('/api/v1')
  expect(await api.listJobs()).toEqual([job])
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/jobs?limit=20')
  expect(await api.listJobs(5)).toEqual([])
  expect(fetch.mock.calls[1][0]).toBe('/api/v1/jobs?limit=5')
  expect(toJob(job)).not.toBe(job)
})

it('propagates list errors', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 503 })))
  await expect(new HttpApiClient('/api/v1').listJobs()).rejects.toThrow()
})

it('provides sample mock cards with links and limits, plus CV sync job ids', async () => {
  await seed()
  await signIn('Admin', ADMIN.email, ADMIN.password)
  const jobs = await mock.listJobs()
  expect(jobs).toHaveLength(3)
  expect(jobs.some((job) => job.status === 'running' && job.kind === 'cv_sync')).toBe(true)
  expect(jobs.some((job) => job.status === 'completed' && job.link?.includes('runId='))).toBe(true)
  expect(await mock.listJobs(1)).toHaveLength(1)
  expect(await mock.syncCvs([1])).toEqual({ jobId: 3 })
})
