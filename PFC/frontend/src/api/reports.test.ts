import { afterEach, expect, it, vi } from 'vitest'
import { HttpApiClient } from './http'
import { emptyExtendedAnalytics } from './reports'

afterEach(() => vi.unstubAllGlobals())

it('fetches all Dashboard panels with one shared-filter request', async () => {
  const payload = { summary: {}, demand: {}, bench: {}, match: {}, extended: emptyExtendedAnalytics() }
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload)))
  vi.stubGlobal('fetch', fetch)
  expect(await new HttpApiClient('/api/v1').analyticsDashboard({ grade: ['B1'], scoreMin: 62 })).toEqual(payload)
  expect(fetch).toHaveBeenCalledOnce()
  const url = new URL(fetch.mock.calls[0]![0] as string, 'http://localhost')
  expect(url.pathname).toBe('/api/v1/analytics/dashboard')
  expect([...url.searchParams.entries()]).toEqual([['grade', 'B1'], ['scoreMin', '62']])
})

it('uses the analytics filter query and authenticated JSON report submission', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(emptyExtendedAnalytics())))
  vi.stubGlobal('fetch', fetch)
  const api = new HttpApiClient('/api/v1')
  await api.analyticsExtended({ grade: ['C1'], scoreMin: 61, runId: 4 })
  const url = new URL(fetch.mock.calls[0]![0] as string, 'http://localhost')
  expect(url.pathname).toBe('/api/v1/analytics/extended')
  expect(url.searchParams.get('grade')).toBe('C1')
  expect(url.searchParams.get('runId')).toBe('4')
  fetch.mockResolvedValueOnce(new Response(JSON.stringify({ user: { user_id: 1, display_name: 'Reviewer', email: 'reviewer@example.com', role: 'Admin', must_change_password: false }, csrf_token: 'csrf', role_at_login: 'Admin', must_change_password: false })))
  await api.login({ role: 'Admin', email: 'reviewer@example.com', password: 'test-only' })
  fetch.mockResolvedValueOnce(new Response(JSON.stringify({ jobId: 42 }), { status: 202 }))
  const body = { report: 'dashboard' as const, scope: 'all' as const, orientation: 'portrait' as const, sections: ['summary' as const] }
  expect(await api.generatePdf(body)).toEqual({ jobId: 42 })
  expect(fetch.mock.calls[2]).toEqual(['/api/v1/reports/pdf', expect.objectContaining({ method: 'POST', credentials: 'same-origin', body: JSON.stringify(body), headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf' }) })])
})
