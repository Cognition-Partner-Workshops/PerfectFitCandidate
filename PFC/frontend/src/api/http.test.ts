import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resolveApiMode } from '../config'
import { ApiError } from './client'
import { HttpApiClient, setUnauthorizedHandler } from './http'
import { fromGrades, toCurrentUser, toDashboardQuery, toEmailDraft, toExportQuery, toGrade } from './mappers'

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

describe('HttpApiClient — OCC per-item CRUD', () => {
  const calls = () => fetchMock.mock.calls.map(([u, i]) => `${(i as RequestInit).method ?? 'GET'} ${String(u)}`)

  it('saves domains as delete plus add and reports the items that did not save', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock
      .mockResolvedValueOnce(json([{ domain: 'old.example.com' }, { domain: 'keep.example.com' }]))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(problem(422, 'VALIDATION_ERROR', 'Invalid domain'))
    const err = await api
      .saveInternalDomains([{ domain: 'keep.example.com' }, { domain: 'bad' }, { domain: 'new.example.com' }])
      .catch((e: unknown) => e)
    expect(calls()).toEqual(['GET /api/v1/occ/internal-domains', 'DELETE /api/v1/occ/internal-domains/old.example.com', 'POST /api/v1/occ/internal-domains'])
    expect((err as Error).message).toContain('Not saved: bad, new.example.com.')
  })

  it('upserts status-map entries by encoded source with the target status priority', async () => {
    const api = new HttpApiClient('/api/v1')
    const bench = [{ code: 'AVAILABLE', label: 'Available', is_excluded: 0, priority: 1, flag_in_gaps: 0, available_from_bench_start: 0 }]
    fetchMock.mockResolvedValueOnce(json(bench)).mockResolvedValueOnce(json([])).mockResolvedValueOnce(json({})).mockResolvedValueOnce(json([]))
    await api.saveStatusMap([{ sourceText: 'On bench / free', canonicalCode: 'AVAILABLE' }])
    expect(calls()[2]).toBe('PUT /api/v1/occ/status-map/On%20bench%20%2F%20free')
    expect(JSON.parse((fetchMock.mock.calls[2][1] as RequestInit).body as string)).toEqual({ canonical_code: 'AVAILABLE', priority: 1, is_excluded: false })
  })

  it('re-syncs stale status-map priorities after a bench status reorder', async () => {
    const api = new HttpApiClient('/api/v1')
    const row = { code: 'AVAILABLE', label: 'Available', is_excluded: 0, priority: 1, flag_in_gaps: 0, available_from_bench_start: 0 }
    fetchMock
      .mockResolvedValueOnce(json([row]))
      .mockResolvedValueOnce(json({}))
      .mockResolvedValueOnce(json([{ ...row, priority: 2 }]))
      .mockResolvedValueOnce(
        json([
          { source_value: 'Free now', canonical_code: 'AVAILABLE', priority: 1, is_excluded: 0 },
          { source_value: 'Ok', canonical_code: 'AVAILABLE', priority: 2, is_excluded: 0 },
        ]),
      )
      .mockResolvedValueOnce(json({}))
    await api.saveBenchStatuses([
      { code: 'AVAILABLE', label: 'Available', eligibility: 'Eligible', priority: 2, tieredPoints: 4, caveat: '', availableFromBenchStart: false },
    ])
    expect(calls()).toEqual([
      'GET /api/v1/occ/bench-statuses',
      'PUT /api/v1/occ/bench-statuses/AVAILABLE',
      'GET /api/v1/occ/bench-statuses',
      'GET /api/v1/occ/status-map',
      'PUT /api/v1/occ/status-map/Free%20now',
    ])
    expect(JSON.parse((fetchMock.mock.calls[4][1] as RequestInit).body as string)).toEqual({ canonical_code: 'AVAILABLE', priority: 2, is_excluded: false })
  })

  it('updates resource-team members by recipient id', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock
      .mockResolvedValueOnce(json([{ recipient_id: 7, email: 'team@example.com', display_name: 'Team', is_active: 1 }]))
      .mockResolvedValueOnce(json({}))
      .mockResolvedValueOnce(json([]))
    await api.saveResourceTeam([{ email: 'team@example.com', displayName: 'Team', active: false, notes: '' }])
    expect(calls()[1]).toBe('PUT /api/v1/occ/resource-team/7')
    expect(JSON.parse((fetchMock.mock.calls[1][1] as RequestInit).body as string)).toEqual({ display_name: 'Team', is_active: false })
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

describe('HttpApiClient — ingestion wiring', () => {
  it('maps the IngestionRun upload reply to the preview', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(
      json({
        ingestion_run_id: 7,
        kind: 'CT',
        status: 'previewed',
        file_name: 'ct.xlsx',
        sheet_name: 'Sheet',
        no_op: false,
        counts: { rows_read: 3, rows_new: 2, rows_updated: 0, rows_unchanged: 0, rows_rejected: 1, rows_incomplete: 0 },
        issues: [{ row_number: 4, column: 'Email Address', message: 'Invalid email', severity: 'error' }, { row_number: null, column: null, message: 'No Group column', severity: 'warning' }],
      }),
    )
    const p = await api.uploadWorkbook('ct', new File(['x'], 'ct.xlsx'))
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/v1/uploads/ct')
    expect(p).toEqual({
      ingestionRunId: 7,
      kind: 'ct',
      fileName: 'ct.xlsx',
      sheet: 'Sheet',
      rowsRead: 3,
      new: 2,
      updated: 0,
      unchanged: 0,
      rejected: 1,
      warnings: ['Row 4 (Email Address): Invalid email', 'No Group column'],
      alreadyImported: false,
    })
  })

  it('pages the demand list and maps rows with neutral JD defaults', async () => {
    const api = new HttpApiClient('/api/v1')
    const row = (id: number) => ({ demand_id: id, code_requisition: `T1000${id}`, id_format_ok: true, position_start_date: null, account_name: 'Acme', code_local_grade: 'B1', demand_request_name: null, region: null, bu: null, owner: null, lifecycle: 'active', occ_pending_review: false })
    fetchMock.mockResolvedValueOnce(json({ items: [row(1)], page: 1, page_size: 200, total: 2 })).mockResolvedValueOnce(json({ items: [row(2)], page: 2, page_size: 200, total: 2 }))
    const d = await api.listDemands()
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toEqual(['/api/v1/demands?page=1&page_size=200', '/api/v1/demands?page=2&page_size=200'])
    expect(d.map((x) => x.codeRequisition)).toEqual(['T10001', 'T10002'])
    expect(d[0]).toMatchObject({ demandId: 1, accountName: 'Acme', jdStatus: 'Not analysed', matches: 0, bestBand: null })
  })
})

describe('HttpApiClient — analytics and export wiring (REQ-OUT-04..09)', () => {
  it('sends only backend DashboardFilters keys, with the demand grade as grade', () => {
    expect(
      toDashboardQuery({ codeRequisition: 'T1', demandGrade: 'B1', personGrade: 'C1', bu: '', scoreMin: 50 }),
    ).toEqual({ codeRequisition: 'T1', grade: 'B1', scoreMin: 50 })
  })

  it('maps result filters onto the export query and leaves emails masked by default', () => {
    const q = new URLSearchParams(
      toExportQuery(
        { codeRequisition: ' T1 ', accountName: 'Acme', location: 'Pune', band: 'Strong', bu: 'X', region: '', candidateType: 'Internal', incompleteIdentity: 'no', mandatorySkills: 'gap', scoreMax: 90 },
        'xlsx',
        false,
      ),
    )
    expect(Object.fromEntries(q)).toEqual({
      format: 'xlsx',
      requisition: 'T1',
      account: 'Acme',
      personLocation: 'Pune',
      band: 'Strong',
      scoreMax: '90',
      bu: 'X',
      candidateType: 'Internal',
      incompleteIdentity: 'no',
      mandatorySkills: 'gap',
    })
    expect(new URLSearchParams(toExportQuery({}, 'csv', true)).get('includeEmails')).toBe('true')
  })

  it('downloads the export with the server file name', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(
      new Response('a,b\n', { headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="PFC-results-run3.csv"' } }),
    )
    const f = await api.exportResults(3, { accountName: 'Acme' }, 'csv', false)
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/v1/match-runs/3/export?format=csv&account=Acme')
    expect(f.fileName).toBe('PFC-results-run3.csv')
    expect(await f.blob.text()).toBe('a,b\n')
  })

  it('surfaces UNMASK_DISABLED when full emails are refused', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(problem(403, 'UNMASK_DISABLED', 'Unmasking is disabled for Analysts'))
    const err = await api.exportResults(3, {}, 'csv', true).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).code).toBe('UNMASK_DISABLED')
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('includeEmails=true')
  })

  it('normalises analytics replies', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock
      .mockResolvedValueOnce(json({ coveragePct: 50, bandDistribution: [{ label: 'Strong', count: 1 }], emailStatusCounts: [], validation: [{ id: 'v1', label: 'Check', passed: true }] }))
      .mockResolvedValueOnce(
        json({
          run: null,
          requisitionsAssessed: 0,
          openRequisitions: 2,
          roleToPersonCombinations: 0,
          rolesWithNoSuitableCandidate: 0,
          weakOnlyRequisitions: 0,
          dataQualityIssues: { total: 1, dl: 1, ct: 0 },
          eligibleBench: 1,
          excludedBench: 1,
          matchCoveragePct: 0,
          emailsWithin24hPct: 0,
        }),
      )
    const m = await api.analyticsMatch({ bu: 'X' })
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/v1/analytics/match?bu=X')
    expect(m.validation[0]).toEqual({ id: 'v1', label: 'Check', passed: true, detail: '' })
    const s = await api.analyticsSummary({})
    expect(s).toMatchObject({ run: null, openRequisitions: 2, dataQualityIssues: { total: 1, dl: 1, ct: 0 } })
  })
})

describe('HttpApiClient — OCC email templates (REQ-EML-03)', () => {
  const row = { email_template_id: 7, name: 'candidate', version: 2, subject: 'Opportunity {role}', body_html: '<p>{candidate_name}</p>', is_default: 1, created_by: 1, created_at: '2026-11-06T09:00:00Z' }

  it('lists latest or all versions and maps rows to camelCase', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(json([row])).mockResolvedValueOnce(json([row]))
    expect(await api.listEmailTemplates(false)).toEqual([
      { emailTemplateId: 7, name: 'candidate', version: 2, subject: 'Opportunity {role}', bodyHtml: '<p>{candidate_name}</p>', isDefault: true, createdBy: 1, createdAt: '2026-11-06T09:00:00Z' },
    ])
    await api.listEmailTemplates(true)
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual(['/api/v1/occ/email-templates', '/api/v1/occ/email-templates?all=true'])
  })

  it('creates a version with the snake_case body and sets the default by id', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(json(AUTH)).mockResolvedValueOnce(json(row, 201)).mockResolvedValueOnce(json(row)).mockResolvedValueOnce(json(row))
    await api.login({ role: 'Admin', email: 'admin@example.com', password: 'x' })
    await api.createEmailTemplate({ name: ' candidate ', subject: 'Opportunity {role}', bodyHtml: '<p>{candidate_name}</p>' })
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/occ/email-templates')
    expect(JSON.parse((fetchMock.mock.calls[1][1] as RequestInit).body as string)).toEqual({ name: 'candidate', subject: 'Opportunity {role}', body_html: '<p>{candidate_name}</p>' })
    expect(headersOf(1)['X-CSRF-Token']).toBe('tok-1')
    expect((await api.setDefaultEmailTemplate(7)).isDefault).toBe(true)
    expect(fetchMock.mock.calls[2]).toMatchObject(['/api/v1/occ/email-templates/7/set-default', { method: 'POST' }])
    expect((await api.getEmailTemplate(7)).bodyHtml).toBe('<p>{candidate_name}</p>')
    expect(fetchMock.mock.calls[3][0]).toBe('/api/v1/occ/email-templates/7')
  })

  it('surfaces UNKNOWN_PLACEHOLDER', async () => {
    const api = new HttpApiClient('/api/v1')
    fetchMock.mockResolvedValueOnce(problem(422, 'UNKNOWN_PLACEHOLDER', 'Unknown email placeholder'))
    await expect(api.createEmailTemplate({ name: 'x', subject: '{nope}', bodyHtml: '' })).rejects.toMatchObject({ code: 'UNKNOWN_PLACEHOLDER' })
  })
})

describe('HttpApiClient — run excluded list (REQ-ELIG-08)', () => {
  it('reads GET /match-runs/{id}/excluded', async () => {
    const api = new HttpApiClient('/api/v1')
    const row = { runId: 4, resourceId: 1, shortName: 'Alex', demandId: null, codeRequisition: null, reasonCode: 'NO_GRADE', detail: 'Person grade not recorded or unknown', scope: 'global' }
    fetchMock.mockResolvedValueOnce(json([row]))
    expect(await api.getRunExcluded(4)).toEqual([row])
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/match-runs/4/excluded')
  })
})
