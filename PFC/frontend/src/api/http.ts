import { ApiError, type ApiClient } from './client'
import { applyChanges } from './crud'
import { ADMIN_SETTINGS } from './mock/fixtures'
import {
  fromAdminSettings,
  fromGrades,
  fromPersonal,
  fromSynonymRelation,
  toAdminSettings,
  toAuditEvent,
  toCurrentUser,
  toDomain,
  toEmailDraft,
  toGrade,
  toLlmModel,
  toMember,
  toOccUser,
  toPersonal,
  toPreview,
  toSynonym,
  type AuditRow,
  type AuthView,
  type EmailRow,
  type GradeRow,
  type ModelRow,
  type PersonalRow,
  type RecipientRow,
  type SynonymRow,
  type UserRow,
  fromBenchStatus,
  fromStatusMapEntry,
  toBenchStatus,
  toStatusMapEntry,
  type BenchRow,
  type StatusMapRow,
  toResource,
  type ResourceItem,
  toUploadPreview,
  type IngestionRunRow,
  toDemand,
  type DemandItem,
  toAnalyticsSummary,
  toBenchAnalytics,
  toDemandAnalytics,
  toMatchAnalytics,
  toExportQuery,
  toEmailTemplate,
  type EmailTemplateRow,
} from './mappers'
import type { BenchStatus, CurrentUser, InternalDomain, PersonalSettings, ProblemDetails, ResourceTeamMember, StatusMapEntry } from './types'

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE'

/** Called on any 401 outside sign-in so the shell can drop the session and route to login (TDD §9.3). */
let unauthorizedHandler: (() => void) | null = null
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn
}

const FRIENDLY: Record<string, string> = {
  INVALID_CREDENTIALS: 'Sign-in failed. Check your role, email and password.',
  ACCOUNT_LOCKED: 'Too many failed sign-ins. The account is temporarily locked.',
  CSRF_INVALID: 'Your session security token expired. Sign in again.',
  PASSWORD_CHANGE_REQUIRED: 'You must change your password before continuing.',
  INVALID_ORIGIN: 'Request blocked: open PFC from the launcher address.',
  INVALID_HOST: 'Request blocked: open PFC from the launcher address.',
  VALIDATION_ERROR: 'Some values are invalid.',
  CONFLICT: 'This change conflicts with existing data.',
  INTERNAL_ERROR: 'Something went wrong in the PFC service.',
}

/** RFC 9457 problem+json → user-facing ProblemDetails (title is the message shown). */
export async function parseProblem(res: Response): Promise<ProblemDetails> {
  let body: Partial<ProblemDetails> & { request_id?: string } = {}
  try {
    body = (await res.json()) as typeof body
  } catch {
    /* non-JSON error body */
  }
  const code = body.code ?? (res.status === 423 ? 'ACCOUNT_LOCKED' : undefined)
  const retry = Number(res.headers.get('Retry-After'))
  const specific = body.detail && body.detail !== body.title ? body.detail : undefined
  const generic = code ? FRIENDLY[code] : undefined
  const detail =
    code === 'VALIDATION_ERROR' && body.detail && body.detail !== 'Invalid request'
      ? body.detail
      : (generic ?? specific ?? body.detail ?? body.title ?? (res.statusText || 'Request failed'))
  return {
    type: body.type,
    title: body.title ?? res.statusText ?? 'Request failed',
    status: res.status,
    detail,
    code,
    retryAfterSeconds: body.retryAfterSeconds ?? (Number.isFinite(retry) && retry > 0 ? retry : undefined),
  }
}

/**
 * Real backend client: cookie session + `X-CSRF-Token` on writes, RFC 9457 errors (TDD.md §9.3, §12).
 */

export class HttpApiClient implements ApiClient {
  private csrfToken: string | null = null
  private readonly base: string

  constructor(base: string) {
    this.base = base.replace(/\/$/, '')
  }

  /** With `raw`, the successful `Response` is returned unread (file downloads). */
  private async request<T>(method: Method, path: string, body?: unknown, raw = false): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' }
    let payload: BodyInit | undefined
    if (body instanceof FormData) payload = body
    else if (body !== undefined) {
      headers['Content-Type'] = 'application/json'
      payload = JSON.stringify(body)
    }
    if (method !== 'GET' && this.csrfToken) headers['X-CSRF-Token'] = this.csrfToken
    let res: Response
    try {
      res = await fetch(`${this.base}${path}`, { method, headers, body: payload, credentials: 'same-origin' })
    } catch {
      throw new ApiError({ title: 'Service unreachable', status: 0, code: 'SERVICE_UNREACHABLE' })
    }
    if (!res.ok) {
      const problem = await parseProblem(res)
      if (res.status === 401 && !['/auth/login', '/auth/me', '/auth/password'].includes(path)) {
        this.csrfToken = null
        unauthorizedHandler?.()
      }
      throw new ApiError(problem)
    }
    if (raw) return res as T
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }

  private remember(view: AuthView): CurrentUser {
    const user = toCurrentUser(view)
    this.csrfToken = user.csrfToken
    this.userId = user.userId
    return user
  }

  private userNames = async () => new Map((await this.listUsers()).map((u) => [u.userId, u]))

  private upload<T>(path: string, file: File): Promise<T> {
    const form = new FormData()
    form.append('file', file)
    return this.request<T>('POST', path, form)
  }

  /** Backend returns `{admin_exists}` (snake_case). */
  getSetupStatus: ApiClient['getSetupStatus'] = async () => {
    const r = await this.request<{ admin_exists?: boolean; adminExists?: boolean }>('GET', '/setup/status')
    return { adminExists: r.admin_exists ?? r.adminExists ?? false }
  }
  setupAdmin: ApiClient['setupAdmin'] = async (req) => {
    await this.request('POST', '/setup/admin', {
      setup_code: req.setupCode,
      email: req.email,
      display_name: req.fullName,
      password: req.password,
    })
  }
  login: ApiClient['login'] = async (req) => this.remember(await this.request('POST', '/auth/login', req))
  logout: ApiClient['logout'] = async () => {
    await this.request('POST', '/auth/logout')
    this.csrfToken = null
  }
  me: ApiClient['me'] = async () => {
    try {
      return this.remember(await this.request<AuthView>('GET', '/auth/me'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null
      throw e
    }
  }
  changePassword: ApiClient['changePassword'] = async (req) =>
    this.remember(
      await this.request('POST', '/auth/password', { current_password: req.currentPassword, new_password: req.newPassword }),
    )

  listDemands: ApiClient['listDemands'] = async () =>
    (await this.allPages<DemandItem>((page) => this.request('GET', `/demands?page=${page}&page_size=200`))).map(toDemand)
  analyzeDemands: ApiClient['analyzeDemands'] = (ids) => this.request('POST', '/demands/analyze', { demandIds: ids })
  getRequirements: ApiClient['getRequirements'] = (id) => this.request('GET', `/demands/${id}/requirements`)
  saveRequirements: ApiClient['saveRequirements'] = (req) =>
    this.request('PUT', `/demands/${req.demandId}/requirements`, req)
  confirmRequirements: ApiClient['confirmRequirements'] = (id) =>
    this.request('POST', `/demands/${id}/requirements/confirm`)
  uploadWorkbook: ApiClient['uploadWorkbook'] = async (kind, file) => toUploadPreview(await this.upload<IngestionRunRow>(`/uploads/${kind}`, file))
  commitIngestion: ApiClient['commitIngestion'] = (id) => this.request('POST', `/ingestion-runs/${id}/commit`)
  discardIngestion: ApiClient['discardIngestion'] = (id) => this.request('POST', `/ingestion-runs/${id}/discard`)

  /** Follows `ResourcePage` paging (page_size ≤ 200) until `total` rows are loaded. */
  private async allPages<T>(fetchPage: (page: number) => Promise<{ items: T[]; total: number }>): Promise<T[]> {
    const items: T[] = []
    for (let page = 1; ; page++) {
      const r = await fetchPage(page)
      items.push(...r.items)
      if (!r.items.length || items.length >= r.total) return items
    }
  }
  listResources: ApiClient['listResources'] = async () =>
    (await this.allPages<ResourceItem>((page) => this.request('GET', `/resources?page=${page}&page_size=200`))).map(toResource)
  listExcludedResources: ApiClient['listExcludedResources'] = async () =>
    (await this.allPages<ResourceItem>((page) => this.request('GET', `/resources/excluded?page=${page}&page_size=200`))).map(toResource)
  syncCvs: ApiClient['syncCvs'] = (ids) => this.request('POST', '/resources/cv-sync', { resource_ids: ids })

  getCandidate: ApiClient['getCandidate'] = (id) => this.request('GET', `/candidates/${id}`)
  resolveIdentity: ApiClient['resolveIdentity'] = (id, r) => this.request('PUT', `/candidates/${id}/identity`, r)

  listMatchRuns: ApiClient['listMatchRuns'] = () => this.request('GET', '/match-runs')
  getMatchRun: ApiClient['getMatchRun'] = (runId) => this.request('GET', `/match-runs/${runId}`)
  getResults: ApiClient['getResults'] = (runId) => this.request('GET', `/match-runs/${runId}/results`)
  addOverride: ApiClient['addOverride'] = (id, o) => this.request('POST', `/match-results/${id}/overrides`, o)
  unmask: ApiClient['unmask'] = async (entity, id, field) =>
    (await this.request<{ value: string }>('POST', '/pii/unmask', { entity, id, field })).value

  private qs(filters: object): string {
    const p = new URLSearchParams()
    Object.entries(filters).forEach(([k, v]) => {
      if (v !== undefined && v !== '') p.set(k, String(v))
    })
    const s = p.toString()
    return s ? `?${s}` : ''
  }
  analyticsSummary: ApiClient['analyticsSummary'] = async (f) => toAnalyticsSummary(await this.request('GET', `/analytics/summary${this.qs(f)}`))
  analyticsDemand: ApiClient['analyticsDemand'] = async (f) => toDemandAnalytics(await this.request('GET', `/analytics/demand${this.qs(f)}`))
  analyticsBench: ApiClient['analyticsBench'] = async (f) => toBenchAnalytics(await this.request('GET', `/analytics/bench${this.qs(f)}`))
  analyticsMatch: ApiClient['analyticsMatch'] = async (f) => toMatchAnalytics(await this.request('GET', `/analytics/match${this.qs(f)}`))
  exportResults: ApiClient['exportResults'] = async (runId, f, format, includeEmails) => {
    const res = await this.request<Response>('GET', `/match-runs/${runId}/export?${toExportQuery(f, format, includeEmails)}`, undefined, true)
    const named = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') ?? '')?.[1]
    return { blob: await res.blob(), fileName: named ?? `PFC-results-run${runId}.${format}` }
  }

  /** POST /emails/drafts needs run_id|demand_id; the latest run is used until matching (#10) exposes it per result. */
  /** Drafts come from the run on screen; by default the newest completed one, since a queued/running run is rejected. */
  createDrafts: ApiClient['createDrafts'] = async (ids, runId) => {
    const run_id = runId ?? (await this.listMatchRuns()).find((r) => r.status === 'completed')?.runId
    const r = await this.request<{ drafts: EmailRow[] }>('POST', '/emails/drafts', { run_id, result_ids: ids })
    return r.drafts.map(toEmailDraft)
  }
  listEmails: ApiClient['listEmails'] = async () => {
    const r = await this.request<{ drafts: EmailRow[] }>('GET', '/emails?limit=500')
    return { items: r.drafts.map(toEmailDraft), transport: { transport: 'Outlook COM', outlookDetected: true } }
  }
  updateEmail: ApiClient['updateEmail'] = async (id, patch) =>
    toEmailDraft(await this.request('PUT', `/emails/${id}`, { subject: patch.subject, body_html: patch.body }))
  approveEmail: ApiClient['approveEmail'] = async (id, confirmResend) =>
    toEmailDraft(await this.request('POST', `/emails/${id}${confirmResend ? '/resend' : '/approve'}`))
  sendEmail: ApiClient['sendEmail'] = async (id) => {
    await this.request('POST', `/emails/${id}/send`)
    return toEmailDraft(await this.request('GET', `/emails/${id}`))
  }

  private userId: number | null = null
  private personalKey = () => `pfc.personal.local.${this.userId ?? 'anon'}`
  /** Fields the backend does not store yet (SPOCs, personal email template) are kept locally, per user. */
  private localPersonal(): PersonalSettings {
    const empty = { enabled: false, watchPath: '', pattern: '' }
    const base: PersonalSettings = { resumeFolderPath: '', autoSyncDl: empty, autoSyncCt: empty, activeModel: '', spoc1: '', spoc2: '', emailTemplate: '', density: 'Comfortable' }
    try {
      return { ...base, ...(JSON.parse(localStorage.getItem(this.personalKey()) ?? '{}') as Partial<PersonalSettings>) }
    } catch {
      return base
    }
  }
  getMySettings: ApiClient['getMySettings'] = async () =>
    toPersonal(await this.request<PersonalRow>('GET', '/settings/me'), this.localPersonal())
  saveMySettings: ApiClient['saveMySettings'] = async (s) => {
    const saved = await this.request<PersonalRow>('PUT', '/settings/me', fromPersonal(s))
    localStorage.setItem(this.personalKey(), JSON.stringify({ spoc1: s.spoc1, spoc2: s.spoc2, emailTemplate: s.emailTemplate }))
    return toPersonal(saved, this.localPersonal())
  }
  listModels: ApiClient['listModels'] = async () => (await this.request<ModelRow[]>('GET', '/llm/models')).map(toLlmModel)
  previewPackage: ApiClient['previewPackage'] = (file) => this.upload('/settings/settings-package/preview', file)
  importPackage: ApiClient['importPackage'] = (source) => this.request('POST', '/settings/settings-package/import', source)
  pendingPackages: ApiClient['pendingPackages'] = () => this.request('GET', '/settings/settings-package/pending')
  rollbackPackage: ApiClient['rollbackPackage'] = () => this.request('POST', '/settings/settings-package/rollback')
  /** Backend rows: `{version, applied_at, applied_by, status, current}`; any non-current applied version can be rolled back to. */
  packageHistory: ApiClient['packageHistory'] = () => this.request('GET', '/settings/settings-package/history')

  listUsers: ApiClient['listUsers'] = async () => (await this.request<UserRow[]>('GET', '/occ/users')).map(toOccUser)
  createUser: ApiClient['createUser'] = async (req) => {
    const r = await this.request<{ user: UserRow; one_time_password: string }>('POST', '/occ/users', {
      email: req.email,
      display_name: req.fullName,
      role: req.role,
      persona: req.persona,
    })
    return { userId: r.user.user_id, oneTimePassword: r.one_time_password }
  }
  resetPassword: ApiClient['resetPassword'] = async (id) => {
    const r = await this.request<{ user_id: number; one_time_password: string }>('POST', `/occ/users/${id}/reset-password`)
    return { userId: r.user_id, oneTimePassword: r.one_time_password }
  }
  unlockUser: ApiClient['unlockUser'] = async (id) => toOccUser(await this.request('POST', `/occ/users/${id}/unlock`))
  updateUser: ApiClient['updateUser'] = async (id, patch) =>
    toOccUser(
      await this.request('PUT', `/occ/users/${id}`, {
        role: patch.role,
        is_active: patch.status === undefined ? undefined : patch.status !== 'Disabled',
      }),
    )

  listBenchStatuses: ApiClient['listBenchStatuses'] = async () =>
    (await this.request<BenchRow[]>('GET', '/occ/bench-statuses')).map(toBenchStatus)
  /** Diffed against the server's list and sent one call at a time (TDD.md §12 per-item CRUD). */
  saveBenchStatuses: ApiClient['saveBenchStatuses'] = async (next) => {
    const same = (a: BenchStatus, b: BenchStatus) => JSON.stringify(fromBenchStatus(a)) === JSON.stringify(fromBenchStatus(b))
    await applyChanges(await this.listBenchStatuses(), next, (s) => s.code, same, {
      create: (s) => this.request('POST', '/occ/bench-statuses', fromBenchStatus(s)),
      update: (s) => this.request('PUT', `/occ/bench-statuses/${encodeURIComponent(s.code)}`, fromBenchStatus(s)),
      remove: (s) => this.request('DELETE', `/occ/bench-statuses/${encodeURIComponent(s.code)}`),
    })
    const statuses = await this.listBenchStatuses()
    // status_map rows carry their own priority/exclusion copied from the target status, which matching uses.
    for (const row of await this.request<StatusMapRow[]>('GET', '/occ/status-map')) {
      const body = fromStatusMapEntry(toStatusMapEntry(row), statuses)
      if (body.canonical_code && (body.priority !== row.priority || body.is_excluded !== !!row.is_excluded))
        await this.request('PUT', `/occ/status-map/${encodeURIComponent(row.source_value)}`, body)
    }
    return statuses
  }
  /** The backend has no unmapped-value list yet, so `unmapped` is empty over HTTP. */
  getStatusMap: ApiClient['getStatusMap'] = async () => ({
    entries: (await this.request<StatusMapRow[]>('GET', '/occ/status-map')).map(toStatusMapEntry),
    unmapped: [],
  })
  saveStatusMap: ApiClient['saveStatusMap'] = async (next) => {
    const statuses = await this.listBenchStatuses()
    const put = (e: StatusMapEntry) =>
      this.request('PUT', `/occ/status-map/${encodeURIComponent(e.sourceText)}`, fromStatusMapEntry(e, statuses))
    await applyChanges((await this.getStatusMap()).entries, next, (e) => e.sourceText, (a, b) => a.canonicalCode === b.canonicalCode, {
      create: put,
      update: put,
      remove: (e) => this.request('DELETE', `/occ/status-map/${encodeURIComponent(e.sourceText)}`),
    })
    return this.getStatusMap()
  }

  listSynonyms: ApiClient['listSynonyms'] = async () => (await this.request<SynonymRow[]>('GET', '/occ/synonyms')).map(toSynonym)
  /** The backend stores one term per row, so each variant is posted separately (REQ-MAT-05). */
  createSynonym: ApiClient['createSynonym'] = async (s) => {
    const rows: SynonymRow[] = []
    try {
      for (const term of s.variants.length ? s.variants : [s.canonical])
        rows.push(
          await this.request('POST', '/occ/synonyms', {
            canonical: s.canonical,
            term,
            relation: fromSynonymRelation(s.relation),
            credit: s.relation === 'Related' ? 0.6 : 1,
            status: 'approved',
          }),
        )
    } catch (e) {
      // All-or-nothing: undo the variants already saved so a retry of the same form succeeds.
      for (const r of rows) await this.request('DELETE', `/occ/synonyms/${r.synonym_id}`).catch(() => undefined)
      throw e
    }
    return { ...toSynonym(rows[0]), variants: rows.map((r) => r.term) }
  }
  updateSynonym: ApiClient['updateSynonym'] = async (id, p) => {
    if (p.status === 'Active') return toSynonym(await this.request('POST', `/occ/synonyms/${id}/approve`))
    if (p.status === 'Rejected') return toSynonym(await this.request('POST', `/occ/synonyms/${id}/reject`))
    const cur = (await this.listSynonyms()).find((x) => x.synonymId === id)
    const next = { ...cur, ...p } as typeof cur & typeof p
    return toSynonym(
      await this.request('PUT', `/occ/synonyms/${id}`, {
        canonical: next.canonical,
        term: next.variants?.[0] ?? next.canonical,
        relation: fromSynonymRelation(next.relation ?? 'Related'),
        credit: next.relation === 'Related' ? 0.6 : 1,
        status: 'approved',
      }),
    )
  }

  listTemplates: ApiClient['listTemplates'] = () => this.request('GET', '/occ/scoring-templates')
  publishTemplate: ApiClient['publishTemplate'] = async (draft) => {
    const created = await this.request<{ templateId: number }>('POST', '/occ/scoring-templates', draft)
    await this.request('POST', `/occ/scoring-templates/${created.templateId}/activate`)
    return this.listTemplates()
  }

  listEmailTemplates: ApiClient['listEmailTemplates'] = async (all) => {
    const params = { all: all || undefined }
    return (await this.request<EmailTemplateRow[]>('GET', `/occ/email-templates${this.qs(params)}`)).map(toEmailTemplate)
  }
  getEmailTemplate: ApiClient['getEmailTemplate'] = async (id) =>
    toEmailTemplate(await this.request<EmailTemplateRow>('GET', `/occ/email-templates/${id}`))
  createEmailTemplate: ApiClient['createEmailTemplate'] = async (d) =>
    toEmailTemplate(await this.request<EmailTemplateRow>('POST', '/occ/email-templates', { name: d.name.trim(), subject: d.subject, body_html: d.bodyHtml }))
  setDefaultEmailTemplate: ApiClient['setDefaultEmailTemplate'] = async (id) =>
    toEmailTemplate(await this.request<EmailTemplateRow>('POST', `/occ/email-templates/${id}/set-default`))

  listResourceTeam: ApiClient['listResourceTeam'] = async () =>
    (await this.request<RecipientRow[]>('GET', '/occ/resource-team')).map(toMember)
  saveResourceTeam: ApiClient['saveResourceTeam'] = async (next) => {
    const rows = await this.request<RecipientRow[]>('GET', '/occ/resource-team')
    const idOf = (m: ResourceTeamMember) => rows.find((r) => r.email.toLowerCase() === m.email.toLowerCase())?.recipient_id
    const same = (a: ResourceTeamMember, b: ResourceTeamMember) => a.displayName === b.displayName && a.active === b.active
    await applyChanges(rows.map(toMember), next, (m) => m.email.toLowerCase(), same, {
      create: async (m) => {
        const r = await this.request<RecipientRow>('POST', '/occ/resource-team', { email: m.email, display_name: m.displayName || null })
        if (!m.active) await this.request('PUT', `/occ/resource-team/${r.recipient_id}`, { is_active: false })
      },
      update: (m) => this.request('PUT', `/occ/resource-team/${idOf(m)}`, { display_name: m.displayName || null, is_active: m.active }),
      remove: (m) => this.request('DELETE', `/occ/resource-team/${idOf(m)}`),
    })
    return this.listResourceTeam()
  }

  listInternalDomains: ApiClient['listInternalDomains'] = async () =>
    (await this.request<{ domain: string }[]>('GET', '/occ/internal-domains')).map(toDomain)
  /** No update route: an edited domain is a delete plus an add. */
  saveInternalDomains: ApiClient['saveInternalDomains'] = async (next) => {
    await applyChanges(await this.listInternalDomains(), next, (d: InternalDomain) => d.domain, () => true, {
      create: (d) => this.request('POST', '/occ/internal-domains', { domain: d.domain }),
      update: async () => undefined,
      remove: (d) => this.request('DELETE', `/occ/internal-domains/${encodeURIComponent(d.domain)}`),
    })
    return this.listInternalDomains()
  }
  previewClassification: ApiClient['previewClassification'] = async (d) =>
    toPreview(await this.request('POST', '/occ/internal-domains/preview', { domains: d.map((x) => x.domain) }))

  lastExportedPackage: ApiClient['lastExportedPackage'] = () => this.request('GET', '/occ/settings-package')
  exportPackage: ApiClient['exportPackage'] = (passphrase) => this.request('POST', '/occ/settings-package/export', { passphrase })

  listApprovals: ApiClient['listApprovals'] = () => this.request('GET', '/occ/approvals')
  decideApproval: ApiClient['decideApproval'] = (e, d) =>
    this.request('PUT', `${e.source === 'CT' ? '/occ/resources' : '/occ/demands'}/${e.targetId}`, d)
  listGrades: ApiClient['listGrades'] = async () => (await this.request<GradeRow[]>('GET', '/occ/grades')).map(toGrade)
  saveGrades: ApiClient['saveGrades'] = async (g) =>
    (await this.request<GradeRow[]>('PUT', '/occ/grades', fromGrades(g))).map(toGrade)
  listOccModels: ApiClient['listOccModels'] = async () =>
    (await this.request<ModelRow[]>('GET', '/occ/llm-models')).map(toLlmModel)
  /** Approval is global (PUT /occ/llm-models/{id}); "default" maps to the caller's active model (PUT /llm/active). */
  updateOccModel: ApiClient['updateOccModel'] = async (id, p) => {
    if (p.approved !== undefined)
      await this.request('PUT', `/occ/llm-models/${encodeURIComponent(id)}`, { approved: p.approved })
    if (p.isDefault) await this.request('PUT', '/llm/active', { model_id: Number(id) })
    return this.listOccModels()
  }
  /** Dates are sent as UTC day bounds (backend requires a timezone); user names are matched client-side. */
  auditLog: ApiClient['auditLog'] = async (q) => {
    const users = await this.userNames()
    const filters = {
      limit: 1000,
      action: q.action,
      entity_type: q.entity,
      from: q.from ? `${q.from}T00:00:00Z` : undefined,
      to: q.to ? `${q.to}T23:59:59Z` : undefined,
    }
    const r = await this.request<{ items: AuditRow[] }>('GET', `/occ/audit-log${this.qs(filters)}`)
    const events = r.items.map((a) => toAuditEvent(a, users))
    const needle = q.user?.toLowerCase()
    return needle ? events.filter((e) => e.user.toLowerCase().includes(needle)) : events
  }
  getAdminSettings: ApiClient['getAdminSettings'] = async () =>
    toAdminSettings(await this.request('GET', '/occ/settings'), ADMIN_SETTINGS)
  saveAdminSettings: ApiClient['saveAdminSettings'] = async (s) =>
    toAdminSettings(await this.request('PUT', '/occ/settings', fromAdminSettings(s)), ADMIN_SETTINGS)
}
