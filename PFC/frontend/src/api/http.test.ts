import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resolveApiMode } from '../config'
import { ApiError } from './client'
import { HttpApiClient, setUnauthorizedHandler } from './http'
import { fromGrades, toCurrentUser, toEmailDraft, toGrade } from './mappers'

const AUTH = {
  user: { user_id: 1, email: 'admin@example.com', display_name: 'OCC Admin', role: 'Admin', must_change_password: false },
  role_at_login: 'Admin',
  must_change_password: false,
  csrf_token: 'tok-1',
}

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })
const problem = (status: number, code: string, detail: string, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify({ type: 'about:blank', title: detail, status, detail, code, request_id: 'r1' }), {
    status,
    headers: { 'Content-Type': 'application/problem+json', ...headers },
  })

let fetchMock: ReturnType<typeof vi.fn>
const headersOf = (call: number) => (fetchMock.mock.calls[call][1] as RequestInit).headers as Record<string, string>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('HttpApiClient — TDD §9.3 session + CSRF', () => {
  it('sends cookies same-origin and X-CSRF-Token from the login response on writes only', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(json(AUTH))
    const user = await api.login({ role: 'Admin', email: 'admin@example.com', password: 'x' })
    expect(user).toMatchObject({ userId: 1, fullName: 'OCC Admin', role: 'Admin', csrfToken: 'tok-1' })
    expect(headersOf(0)['X-CSRF-Token']).toBeUndefined()

    fetchMock.mockResolvedValueOnce(json([]))
    await api.listGrades()
    expect(headersOf(1)['X-CSRF-Token']).toBeUndefined()
    expect((fetchMock.mock.calls[1][1] as RequestInit).credentials).toBe('same-origin')

    fetchMock.mockResolvedValueOnce(json([{ code: 'A3', ordinal: 1, active: 1 }]))
    await api.saveGrades([{ code: 'A3', order: 1, retired: false }])
    expect(fetchMock.mock.calls[2][0]).toBe('/api/v1/occ/grades')
    expect(headersOf(2)['X-CSRF-Token']).toBe('tok-1')
    expect(JSON.parse((fetchMock.mock.calls[2][1] as RequestInit).body as string)).toEqual([{ code: 'A3', active: true }])
  })

  it('refreshes the CSRF token after a password change', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(json(AUTH)).mockResolvedValueOnce(json({ ...AUTH, csrf_token: 'tok-2' }))
    await api.login({ role: 'Admin', email: 'admin@example.com', password: 'x' })
    await api.changePassword({ currentPassword: 'old', newPassword: 'new' })
    expect(JSON.parse((fetchMock.mock.calls[1][1] as RequestInit).body as string)).toEqual({
      current_password: 'old',
      new_password: 'new',
    })
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }))
    await api.logout()
    expect(headersOf(2)['X-CSRF-Token']).toBe('tok-2')
  })
})

describe('HttpApiClient — RFC 9457 problem+json', () => {
  it('parses code and shows a friendly message', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(problem(403, 'CSRF_INVALID', 'Invalid CSRF token'))
    const err = await api.saveGrades([]).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(403)
    expect((err as ApiError).code).toBe('CSRF_INVALID')
    expect((err as ApiError).message).toBe('Your session security token expired. Sign in again.')
  })

  it('keeps a specific validation detail', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(problem(422, 'VALIDATION_ERROR', 'Invalid or reserved settings key'))
    const err = (await api.getAdminSettings().catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('Invalid or reserved settings key')
  })

  it('maps 423 to ACCOUNT_LOCKED with Retry-After seconds', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ title: 'Locked', status: 423 }), { status: 423, headers: { 'Retry-After': '600' } }),
    )
    const err = (await api.login({ role: 'Analyst', email: 'a@example.com', password: 'x' }).catch((e: unknown) => e)) as ApiError
    expect(err.code).toBe('ACCOUNT_LOCKED')
    expect(err.problem.retryAfterSeconds).toBe(600)
  })

  it('reports an unreachable service as status 0', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const err = (await api.listUsers().catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(0)
  })
})

describe('HttpApiClient — 401 handling', () => {
  it('calls the unauthorized handler (→ login) on 401 from a protected route', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(problem(401, 'AUTH_REQUIRED', 'Authentication required'))
    await expect(api.listUsers()).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not redirect on a failed sign-in or an absent session', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(problem(401, 'INVALID_CREDENTIALS', 'invalid credentials'))
    await expect(api.login({ role: 'Admin', email: 'a@example.com', password: 'x' })).rejects.toThrow(/Sign-in failed/)
    fetchMock.mockResolvedValueOnce(problem(401, 'AUTH_REQUIRED', 'Authentication required'))
    expect(await api.me()).toBeNull()
    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})

describe('HttpApiClient — review fixes', () => {
  it('posts one synonym row per variant', async () => {
    const api = new HttpApiClient('/api/v1')
    const row = (id: number, term: string) => json({ synonym_id: id, canonical: 'Java', term, relation: 'equivalent', status: 'approved', created_by: 1 })
    fetchMock.mockResolvedValueOnce(row(1, 'J2EE')).mockResolvedValueOnce(row(2, 'JEE'))
    const s = await api.createSynonym({ canonical: 'Java', variants: ['J2EE', 'JEE'], relation: 'Exact synonym' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(s.variants).toEqual(['J2EE', 'JEE'])
  })

  it('undoes saved variants when a later one fails', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock
      .mockResolvedValueOnce(json({ synonym_id: 5, canonical: 'Java', term: 'J2EE', relation: 'equivalent', status: 'approved', created_by: 1 }))
      .mockResolvedValueOnce(problem(409, 'CONFLICT', 'Synonym exists'))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    await expect(api.createSynonym({ canonical: 'Java', variants: ['J2EE', 'JEE'], relation: 'Exact synonym' })).rejects.toBeInstanceOf(ApiError)
    expect(fetchMock.mock.calls[2][0]).toBe('/api/v1/occ/synonyms/5')
    expect((fetchMock.mock.calls[2][1] as RequestInit).method).toBe('DELETE')
  })

  it('keeps local-only settings per user and only after the server accepts the save', async () => {
    localStorage.clear()
    const api = new HttpApiClient('/api/v1')
    const row = { resume_root_path: null, watch: { dl: { enabled: false, path: null, pattern: 'DL_*.xlsx' }, ct: { enabled: false, path: null, pattern: 'CT_*.xlsx' } }, active_model_id: null, density: 'comfortable' }
    fetchMock.mockResolvedValueOnce(json(AUTH))
    await api.login({ role: 'Admin', email: 'admin@example.com', password: 'x' })
    fetchMock.mockResolvedValueOnce(json(row))
    const s = await api.getMySettings()
    fetchMock.mockResolvedValueOnce(problem(422, 'VALIDATION_ERROR', 'Invalid request'))
    await expect(api.saveMySettings({ ...s, spoc1: 'Rejected SPOC' })).rejects.toBeInstanceOf(ApiError)
    expect(localStorage.getItem('pfc.personal.local.1')).toBeNull()
    fetchMock.mockResolvedValueOnce(json(row))
    expect((await api.saveMySettings({ ...s, spoc1: 'SPOC A' })).spoc1).toBe('SPOC A')
    fetchMock.mockResolvedValueOnce(json({ ...AUTH, user: { ...AUTH.user, user_id: 2 } })).mockResolvedValueOnce(json(row))
    await api.login({ role: 'Analyst', email: 'b@example.com', password: 'x' })
    expect((await api.getMySettings()).spoc1).toBe('')
  })

  it('sends the export passphrase and the pending-package source', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(json({})).mockResolvedValueOnce(json([]))
    await api.exportPackage('pass-1')
    await api.importPackage({ pendingId: 'a'.repeat(64) })
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({ passphrase: 'pass-1' })
    expect(JSON.parse((fetchMock.mock.calls[1][1] as RequestInit).body as string)).toEqual({ pendingId: 'a'.repeat(64) })
  })
})

describe('mappers', () => {
  it('masks the draft recipient email', () => {
    const d = toEmailDraft({ message_id: 1, demand_id: 1, resource_id: 1, result_id: 1, rank: 1, selected: true, to_address: 'jane.doe@example.com', cc: [], subject: 's', body_html: 'b', status: 'Draft', created_at: 'x', sent_at: null, last_error: null })
    expect(d.toMasked).not.toContain('jane.doe')
  })
  it('flattens AuthView using the role chosen at login', () => {
    expect(toCurrentUser({ ...AUTH, role_at_login: 'Analyst' }).role).toBe('Analyst')
  })
  it('round-trips grades (active ↔ retired, ordinal ↔ order)', () => {
    const g = [toGrade({ code: 'B1', ordinal: 2, active: 0 }), toGrade({ code: 'A3', ordinal: 1, active: 1 })]
    expect(fromGrades(g)).toEqual([
      { code: 'A3', active: true },
      { code: 'B1', active: false },
    ])
  })
})

describe('resolveApiMode', () => {
  it('defaults to http, mock only by explicit flag', () => {
    expect(resolveApiMode({ DEV: false }, '?mock=1')).toBe('http')
    expect(resolveApiMode({ DEV: true }, '')).toBe('http')
    expect(resolveApiMode({ DEV: true }, '?mock=1')).toBe('mock')
    expect(resolveApiMode({ VITE_PFC_API: 'mock' }, '')).toBe('mock')
    expect(resolveApiMode({ VITE_PFC_API: 'http', DEV: true }, '?mock=1')).toBe('http')
  })
})

describe('HttpApiClient — resources and match runs (ingestion #12, matching #10)', () => {
  const item = (id: number, eligibility: string) => ({
    resource_id: id, short_name: `R${id}`, email_address: 'jane.doe@example.com', grade: 'C1', location: 'Pune',
    bench_status: 'Available', bench_status_code: 'AVAILABLE', skills_raw: 'Java, Spring; SQL', available_capacity: 'Y',
    bench_start_date: '2026-10-01', bench_end_date: null, group_name: 'G1', lifecycle: 'active', eligibility,
    exclusion_reasons: eligibility === 'excluded' ? ['Status excluded'] : [], pending_review: false, unmapped_status: false, occ_pending_review: false,
  })

  it('unwraps and pages through ResourcePage for excluded resources', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock
      .mockResolvedValueOnce(json({ items: [item(1, 'excluded'), item(2, 'excluded')], page: 1, page_size: 2, total: 3 }))
      .mockResolvedValueOnce(json({ items: [item(3, 'excluded')], page: 2, page_size: 2, total: 3 }))
    const rows = await api.listExcludedResources()
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toEqual([
      '/api/v1/resources/excluded?page=1&page_size=200',
      '/api/v1/resources/excluded?page=2&page_size=200',
    ])
    expect(rows.map((r) => r.resourceId)).toEqual([1, 2, 3])
    expect(rows[0]).toMatchObject({ eligibility: 'excluded', skills: ['Java', 'Spring', 'SQL'], availableCapacity: 'Y', exclusionReasons: ['Status excluded'] })
    expect(rows[0]?.emailAddressMasked).not.toContain('jane.doe')
  })

  it('stops paging on an empty page and fetches a single run by id', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(json({ items: [], page: 1, page_size: 200, total: 5 })).mockResolvedValueOnce(json({ runId: 9, status: 'running' }))
    expect(await api.listResources()).toEqual([])
    expect((await api.getMatchRun(9)).status).toBe('running')
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe('/api/v1/match-runs/9')
  })

  it('marks identity from the email, not the bench-date pending_review flag', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(
      json({ items: [{ ...item(1, 'eligible'), pending_review: true }, { ...item(2, 'eligible'), email_address: null }], page: 1, page_size: 200, total: 2 }),
    )
    expect((await api.listResources()).map((r) => r.identityStatus)).toEqual(['complete', 'incomplete'])
  })

  it('drafts emails from the given run, else the newest completed one rather than a running run', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock
      .mockResolvedValueOnce(json([{ runId: 3, status: 'running' }, { runId: 2, status: 'completed' }]))
      .mockResolvedValueOnce(json({ drafts: [], issues: [] }))
      .mockResolvedValueOnce(json({ drafts: [], issues: [] }))
    await api.createDrafts([7])
    await api.createDrafts([8], 1)
    const bodies = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST').map(([, init]) => JSON.parse(String(init?.body)))
    expect(bodies).toEqual([{ run_id: 2, result_ids: [7] }, { run_id: 1, result_ids: [8] }])
  })
})
