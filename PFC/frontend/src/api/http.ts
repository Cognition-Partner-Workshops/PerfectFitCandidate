import { ApiError, type ApiClient } from './client'
import type { CurrentUser, ProblemDetails } from './types'

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE'

/**
 * Real backend client: cookie session + `X-CSRF-Token` on writes, RFC 9457 errors (TDD.md §9.3, §12).
 */
export class HttpApiClient implements ApiClient {
  private csrfToken: string | null = null
  private readonly base: string

  constructor(base: string) {
    this.base = base.replace(/\/$/, '')
  }

  private async request<T>(method: Method, path: string, body?: unknown): Promise<T> {
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
      let problem: ProblemDetails = { title: res.statusText || 'Request failed', status: res.status }
      try {
        problem = { ...problem, ...((await res.json()) as Partial<ProblemDetails>) }
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(problem)
    }
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }

  private remember(user: CurrentUser): CurrentUser {
    this.csrfToken = user.csrfToken
    return user
  }

  private upload<T>(path: string, file: File): Promise<T> {
    const form = new FormData()
    form.append('file', file)
    return this.request<T>('POST', path, form)
  }

  getSetupStatus: ApiClient['getSetupStatus'] = () => this.request('GET', '/setup/status')
  setupAdmin: ApiClient['setupAdmin'] = (req) => this.request('POST', '/setup/admin', req)
  login: ApiClient['login'] = async (req) => this.remember(await this.request('POST', '/auth/login', req))
  logout: ApiClient['logout'] = async () => {
    await this.request('POST', '/auth/logout')
    this.csrfToken = null
  }
  me: ApiClient['me'] = async () => {
    try {
      return this.remember(await this.request<CurrentUser>('GET', '/auth/me'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null
      throw e
    }
  }
  changePassword: ApiClient['changePassword'] = async (req) =>
    this.remember(await this.request('POST', '/auth/password', req))

  listDemands: ApiClient['listDemands'] = () => this.request('GET', '/demands')
  analyzeDemands: ApiClient['analyzeDemands'] = (ids) => this.request('POST', '/demands/analyze', { demandIds: ids })
  getRequirements: ApiClient['getRequirements'] = (id) => this.request('GET', `/demands/${id}/requirements`)
  saveRequirements: ApiClient['saveRequirements'] = (req) =>
    this.request('PUT', `/demands/${req.demandId}/requirements`, req)
  confirmRequirements: ApiClient['confirmRequirements'] = (id) =>
    this.request('POST', `/demands/${id}/requirements/confirm`)
  uploadWorkbook: ApiClient['uploadWorkbook'] = (kind, file) => this.upload(`/uploads/${kind}`, file)
  commitIngestion: ApiClient['commitIngestion'] = (id) => this.request('POST', `/ingestion-runs/${id}/commit`)
  discardIngestion: ApiClient['discardIngestion'] = (id) => this.request('POST', `/ingestion-runs/${id}/discard`)

  listResources: ApiClient['listResources'] = () => this.request('GET', '/resources')
  listExcludedResources: ApiClient['listExcludedResources'] = () => this.request('GET', '/resources/excluded')
  syncCvs: ApiClient['syncCvs'] = (ids) => this.request('POST', '/resources/cv-sync', { ids })

  getCandidate: ApiClient['getCandidate'] = (id) => this.request('GET', `/candidates/${id}`)
  resolveIdentity: ApiClient['resolveIdentity'] = (id, r) => this.request('PUT', `/candidates/${id}/identity`, r)

  listMatchRuns: ApiClient['listMatchRuns'] = () => this.request('GET', '/match-runs')
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
  analyticsSummary: ApiClient['analyticsSummary'] = (f) => this.request('GET', `/analytics/summary${this.qs(f)}`)
  analyticsDemand: ApiClient['analyticsDemand'] = (f) => this.request('GET', `/analytics/demand${this.qs(f)}`)
  analyticsBench: ApiClient['analyticsBench'] = (f) => this.request('GET', `/analytics/bench${this.qs(f)}`)
  analyticsMatch: ApiClient['analyticsMatch'] = (f) => this.request('GET', `/analytics/match${this.qs(f)}`)

  createDrafts: ApiClient['createDrafts'] = (ids) => this.request('POST', '/emails/drafts', { resultIds: ids })
  listEmails: ApiClient['listEmails'] = () => this.request('GET', '/emails')
  updateEmail: ApiClient['updateEmail'] = (id, patch) => this.request('PUT', `/emails/${id}`, patch)
  approveEmail: ApiClient['approveEmail'] = (id, confirmResend) =>
    this.request('POST', `/emails/${id}/approve`, { confirmResend: Boolean(confirmResend) })
  sendEmail: ApiClient['sendEmail'] = (id) => this.request('POST', `/emails/${id}/send`)

  getMySettings: ApiClient['getMySettings'] = () => this.request('GET', '/settings/me')
  saveMySettings: ApiClient['saveMySettings'] = (s) => this.request('PUT', '/settings/me', s)
  listModels: ApiClient['listModels'] = () => this.request('GET', '/llm/models')
  previewPackage: ApiClient['previewPackage'] = (file) => this.upload('/settings/settings-package/preview', file)
  importPackage: ApiClient['importPackage'] = (previewId) =>
    this.request('POST', '/settings/settings-package/import', { previewId })
  rollbackPackage: ApiClient['rollbackPackage'] = () => this.request('POST', '/settings/settings-package/rollback')
  packageHistory: ApiClient['packageHistory'] = () => this.request('GET', '/settings/settings-package/history')

  listUsers: ApiClient['listUsers'] = () => this.request('GET', '/occ/users')
  createUser: ApiClient['createUser'] = (req) => this.request('POST', '/occ/users', req)
  resetPassword: ApiClient['resetPassword'] = (id) => this.request('POST', `/occ/users/${id}/reset-password`)
  unlockUser: ApiClient['unlockUser'] = (id) => this.request('POST', `/occ/users/${id}/unlock`)
  updateUser: ApiClient['updateUser'] = (id, patch) => this.request('PUT', `/occ/users/${id}`, patch)

  listBenchStatuses: ApiClient['listBenchStatuses'] = () => this.request('GET', '/occ/bench-statuses')
  saveBenchStatuses: ApiClient['saveBenchStatuses'] = (s) => this.request('PUT', '/occ/bench-statuses', s)
  getStatusMap: ApiClient['getStatusMap'] = () => this.request('GET', '/occ/status-map')
  saveStatusMap: ApiClient['saveStatusMap'] = (e) => this.request('PUT', '/occ/status-map', e)

  listSynonyms: ApiClient['listSynonyms'] = () => this.request('GET', '/occ/synonyms')
  createSynonym: ApiClient['createSynonym'] = (s) => this.request('POST', '/occ/synonyms', s)
  updateSynonym: ApiClient['updateSynonym'] = (id, p) => this.request('PUT', `/occ/synonyms/${id}`, p)

  listTemplates: ApiClient['listTemplates'] = () => this.request('GET', '/occ/scoring-templates')
  publishTemplate: ApiClient['publishTemplate'] = async (draft) => {
    const created = await this.request<{ templateId: number }>('POST', '/occ/scoring-templates', draft)
    await this.request('POST', `/occ/scoring-templates/${created.templateId}/activate`)
    return this.listTemplates()
  }

  listResourceTeam: ApiClient['listResourceTeam'] = () => this.request('GET', '/occ/resource-team')
  saveResourceTeam: ApiClient['saveResourceTeam'] = (m) => this.request('PUT', '/occ/resource-team', m)

  listInternalDomains: ApiClient['listInternalDomains'] = () => this.request('GET', '/occ/internal-domains')
  saveInternalDomains: ApiClient['saveInternalDomains'] = (d) => this.request('PUT', '/occ/internal-domains', d)
  previewClassification: ApiClient['previewClassification'] = (d) =>
    this.request('POST', '/occ/internal-domains/preview', d)

  lastExportedPackage: ApiClient['lastExportedPackage'] = () => this.request('GET', '/occ/settings-package')
  exportPackage: ApiClient['exportPackage'] = () => this.request('POST', '/occ/settings-package/export')
}
