/**
 * In-browser mock of the `/api/v1` backend (TDD.md §12) used until the real backend is wired in
 * via `VITE_PFC_API=http`. State is kept in localStorage so the mock survives page reloads.
 * Password hashing here is a non-cryptographic placeholder for the mock only — the backend uses
 * Argon2id (TDD.md §9.2).
 */
import { ApiError, type ApiClient, type EmailList, type StatusMapData } from '../client'
import { applyChanges } from '../crud'
import type {
  DocumentSummary,
  CandidateDetail,
  CandidateClassification,
  CandidateType,
  CvFact,
  CvFacts,
  CurrentUser,
  DashboardFilters,
  Demand,
  JdStatus,
  EmailDraft,
  JdRequirements,
  MatchOverride,
  MatchResult,
  MatchRun,
  OccUser,
  PackageImportRecord,
  PackagePreview,
  PackageSource,
  PendingPackage,
  PersonalSettings,
  ProblemDetails,
  Resource,
  ResourceTeamMember,
  Role,
  ScoringTemplate,
  EmailTemplateDraft,
  EmailTemplateVersion,
  SettingsPackageInfo,
  Synonym,
  UserStatus,
  BenchStatus,
  StatusMapEntry,
  ValidationCheck,
  ResultFilters,
  AdminSettings,
  ApprovalDecision,
  ApprovalEntry,
  AuditEvent,
  AuditQuery,
  Grade,
  LlmModel as OccLlmModel,
  MatchRunRequest,
} from '../types'
import {
  ADMIN_SETTINGS,
  APPROVALS,
  GRADE_SEED,
  BENCH_STATUSES,
  DEFAULT_EMAIL_TEMPLATE,
  DEMANDS,
  MODELS,
  RESOURCES,
  RESOURCE_TEAM,
  STATUS_MAP,
  SYNONYMS,
  type DemandSeed,
  type RawResource,
} from './fixtures'
import { classify, scoreAll, toResource } from './scorer'
import { benchAnalytics, demandAnalytics, matchAnalytics, summarise } from '../../lib/analytics'
import { applyResultFilters } from '../../lib/filters'
import { resultsToCsv } from '../../lib/csv'
import { checkPassword } from '../../lib/password'
import { checkTemplate } from '../../lib/occ'
import { formatPct } from '../../lib/format'
import { maskEmail, maskPhone } from '../../lib/mask'

// Bump when persisted mock shapes or IDs change so stale state is discarded.
const STORAGE_KEY = 'pfc.mock.v5'

interface MockUser extends OccUser {
  passwordHash: string
  mustChangePassword: boolean
  failures: number
  lockedUntil: number | null
}

interface MockAuditEntry {
  at: string
  summary: string
  candidateId?: number
  user?: string
  role?: AuditEvent['role']
  action?: string
  entity?: string
  details?: string
}

interface MockState {
  summaries?: Record<string, DocumentSummary>
  classificationOverrides?: Record<number, CandidateClassification>
  removedExtractions?: number[]
  /** Reasons data changed since the mock run finished. The mock never completes a new run, so they are only cleared on reset. */
  dataChanges?: string[]
  users: MockUser[]
  sessionUserId: number | null
  setupCode: string
  demands: DemandSeed[]
  requirements: Record<number, JdRequirements>
  raws: RawResource[]
  statuses: BenchStatus[]
  statusMap: StatusMapEntry[]
  synonyms: Synonym[]
  templates: ScoringTemplate[]
  emailTemplates: EmailTemplateVersion[]
  resourceTeam: ResourceTeamMember[]
  domains: string[]
  emails: EmailDraft[]
  overrides: Record<number, MatchOverride>
  settings: PersonalSettings
  installedPackageVersion: number
  packageHistory: PackageImportRecord[]
  previews: PackagePreview[]
  lastExport: SettingsPackageInfo | null
  audit: MockAuditEntry[]
  approvals: ApprovalEntry[]
  grades: Grade[]
  models: OccLlmModel[]
  adminSettings: AdminSettings
  nextId: number
}

/** FNV-1a — mock placeholder only, never a real password hash. */
function mockHash(value: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < value.length; i++) {
    h ^= value.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16)
}

function randomToken(length: number, alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'): string {
  const bytes = new Uint32Array(length)
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes)
  else for (let i = 0; i < length; i++) bytes[i] = Math.floor(Math.random() * 2 ** 32)
  return [...bytes].map((b) => alphabet[b % alphabet.length]).join('')
}

/** One-time password that satisfies both policies (≥ 14 chars, 4 character classes). */
function oneTimePassword(): string {
  return `${randomToken(4, 'ABCDEFGHJKLMNPQRSTUVWXYZ')}-${randomToken(5, 'abcdefghijkmnopqrstuvwxyz')}-${randomToken(4, '23456789')}!`
}

const now = () => new Date()
const iso = () => now().toISOString()

function fail(status: number, title: string, code: string, extra: Partial<ProblemDetails> = {}): never {
  throw new ApiError({ type: 'about:blank', title, status, code, detail: title, ...extra })
}

const DEFAULT_TEMPLATE: ScoringTemplate = {
  templateId: 1,
  version: 3,
  status: 'active',
  weights: { skills: 70, grade: 10, location: 5, benchStart: 5, benchEnd: 5, status: 5 },
  adjacentGradePoints: 7,
  statusScoreMode: 'binary',
  bands: { strong: 90, potential: 70, avg: 61 },
  changeNote: 'Baseline 70/10/5/5/5/5 (D-02)',
  createdAt: '2026-11-03T16:40:00Z',
  createdBy: 'OCC Admin',
}

function initialState(): MockState {
  const state: MockState = {
    users: [],
    sessionUserId: null,
    setupCode: randomToken(8, 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'),
    demands: structuredClone(DEMANDS),
    requirements: {},
    raws: structuredClone(RESOURCES),
    statuses: structuredClone(BENCH_STATUSES),
    statusMap: structuredClone(STATUS_MAP),
    synonyms: structuredClone(SYNONYMS),
    templates: [
      { ...DEFAULT_TEMPLATE, templateId: 0, version: 2, status: 'retired', weights: { ...DEFAULT_TEMPLATE.weights, skills: 65, grade: 15 }, changeNote: 'Pilot weights', createdAt: '2026-10-20T10:00:00Z' },
      DEFAULT_TEMPLATE,
    ],
    resourceTeam: structuredClone(RESOURCE_TEAM),
    domains: ['example.com'],
    emails: [],
    overrides: {},
    settings: {
      resumeFolderPath: 'C:\\Users\\analyst\\OneDrive\\Bench CVs',
      autoSyncDl: { enabled: false, watchPath: '', pattern: 'DL_*.xlsx' },
      autoSyncCt: { enabled: false, watchPath: '', pattern: 'CT_*.xlsx' },
      activeModel: 'qwen3-4b',
      spoc1: 'SPOC-1',
      spoc2: 'SPOC-2',
      emailTemplate: DEFAULT_EMAIL_TEMPLATE,
      density: 'Comfortable',
    },
    installedPackageVersion: 6,
    packageHistory: [
      { version: 6, importedAt: '2026-11-01T09:00:00Z', importedBy: 'OCC Admin', fileName: 'PFC-settings-v6.pfcpkg', canRollback: false, status: 'applied', isCurrent: true },
    ],
    previews: [],
    lastExport: null,
    audit: [
      { at: '2026-11-03T16:40:00Z', summary: 'Scoring template v3 published', user: 'OCC Admin', role: 'Admin', action: 'publish', entity: 'scoring_template' },
      { at: '2026-11-01T09:00:00Z', summary: 'Settings package v6 imported', user: 'OCC Admin', role: 'Admin', action: 'import', entity: 'settings_package' },
      { at: '2026-10-30T08:15:00Z', summary: 'Nightly backup completed', user: 'System', role: 'System', action: 'backup', entity: 'system' },
    ],
    approvals: structuredClone(APPROVALS),
    grades: structuredClone(GRADE_SEED),
    models: structuredClone(MODELS),
    adminSettings: { ...ADMIN_SETTINGS },
    nextId: 1000,
    emailTemplates: [
      {
        emailTemplateId: 1,
        name: 'candidate_default',
        version: 1,
        subject: 'Opportunity {requisition} – {role}',
        bodyHtml: "<p>Hi {candidate_name},</p>\n<p>You have been shortlisted for {role} ({requisition}), with a match score of {match_pct}.</p>\n<p>{analysis}</p>\n<p><strong>Gaps to consider:</strong><br>{gaps}</p>\n<p><strong>Profile improvement pointers:</strong><br>{pointers}</p>\n<p>Please update your CV against the attached job description and share it with {spoc_1} and {spoc_2}.</p>",
        isDefault: true,
        createdBy: null,
        createdAt: '2026-10-20T10:00:00Z',
      },
    ],
  }
  return state
}

function seedEmails(client: MockApiClient): void {
  const s = client.state
  if (s.emails.length) return
  const results = client.computeResults()
  const pick = (req: string) => results.filter((r) => r.codeRequisition === req)
  for (const r of [...pick('T1A123'), ...pick('T2B456'), ...pick('T2C900')]) s.emails.push(client.draftFor(r))
  for (const r of pick('T1E333')) {
    const d = client.draftFor(r)
    d.status = r.rank <= 3 ? 'Sent' : r.rank === 4 ? 'Failed' : 'Approved'
    if (d.status === 'Sent') d.sentAt = '2026-11-04T15:20:00Z'
    if (d.status === 'Failed') d.failureReason = 'Outlook security prompt declined'
    s.emails.push(d)
  }
}

function mockCvFacts(shortName: string, skills: string[], phoneMasked: string | null): CvFacts {
  const fact = (value: string, evidence: string, at: number, source: CvFact['source'] = 'rule'): CvFact => ({
    value,
    evidence,
    span: [at, at + evidence.length],
    source,
  })
  const skill = skills[0]
  return {
    name: fact(shortName, `Name: ${shortName}`, 0),
    phone: phoneMasked ? fact(phoneMasked, `Phone: ${phoneMasked}`, 40) : null,
    experience: skill ? [fact(`5 years ${skill}`, `5 years of hands-on ${skill} delivery for financial-services clients.`, 80, 'llm')] : [],
    qualifications: [],
    projects: skill ? [fact(`${skill} platform migration`, `Led the ${skill} platform migration for a retail bank.`, 200, 'llm')] : [],
  }
}


/** No requirements exist for the demand's current JD version (backend: no draft or confirmed row). */
const needsAnalysis = (d: { jdStatus: JdStatus }) => d.jdStatus === 'Not analysed' || d.jdStatus === 'JD changed'

export class MockApiClient implements ApiClient {
  private async summarySource(kind: 'candidate' | 'demand', id: number) {
    this.currentUser()
    if (kind === 'candidate') {
      const candidate = await this.getCandidate(id)
      const cv = candidate.cvVersions[0]
      if (!cv || this.state.removedExtractions?.includes(cv.cvVersionId)) fail(409, 'No source document is recorded', 'SUMMARY_NO_SOURCE')
      const fact = cv.facts?.experience[0] ?? cv.facts?.qualifications[0]
      if (!fact?.evidence) fail(409, 'No source text is recorded', 'SUMMARY_NO_SOURCE')
      return { version: cv.cvVersionId, text: fact.evidence }
    }
    const demand = this.state.demands.find((d) => d.demandId === id)
    if (!demand?.jdText?.trim()) fail(409, 'No source document is recorded', 'SUMMARY_NO_SOURCE')
    const version = Array.from(demand.jdText).reduce((hash, char) => (Math.imul(hash, 31) + char.charCodeAt(0)) >>> 0, 1)
    return { version, text: demand.jdText }
  }

  private async readSummary(kind: 'candidate' | 'demand', id: number): Promise<DocumentSummary> {
    this.currentUser()
    const saved = this.state.summaries?.[`${kind}:${id}`]
    if (!saved) fail(404, 'No summary has been generated', 'SUMMARY_NOT_FOUND')
    let stale = true
    try { stale = (await this.summarySource(kind, id)).version !== saved.sourceVersionId } catch { /* Removed source leaves the saved reading aid stale. */ }
    return this.done({ ...saved, stale })
  }

  private async writeSummary(kind: 'candidate' | 'demand', id: number): Promise<DocumentSummary> {
    const source = await this.summarySource(kind, id)
    const text = source.text.slice(0, 300)
      .replace(/[\w.+-]+@[\w.-]+/g, (email) => maskEmail(email) ?? '***')
      .replace(/\+?(?:\(\d{1,4}\)|\d)[\d ().-]{5,}\d/g, (phone) => /^(19|20)\d{2}\s*[-–—/]\s*(19|20)\d{2}$/.test(phone) ? phone : '***' + phone.slice(-2))
    const summary: DocumentSummary = {
      sentences: [{ text, evidence: text }], model: 'mock-summary', createdAt: new Date().toISOString(),
      stale: false, droppedUngrounded: 0, sourceVersionId: source.version,
    }
    this.state.summaries ??= {}
    this.state.summaries[`${kind}:${id}`] = summary
    this.save()
    return this.done(summary)
  }

  getCandidateSummary = (id: number) => this.readSummary('candidate', id)
  generateCandidateSummary = (id: number) => this.writeSummary('candidate', id)
  getDemandSummary = (id: number) => this.readSummary('demand', id)
  generateDemandSummary = (id: number) => this.writeSummary('demand', id)
  state: MockState
  private readonly persist: boolean
  private readonly latencyMs: number

  constructor(options: { persist?: boolean; latencyMs?: number } = {}) {
    this.persist = options.persist ?? true
    this.latencyMs = options.latencyMs ?? (import.meta.env.MODE === 'test' ? 0 : 120)
    this.state = this.load() ?? initialState()
    seedEmails(this)
    this.save()
    if (import.meta.env.MODE !== 'test' && !this.state.users.some((u) => u.role === 'Admin')) {
      console.info(`[PFC mock] First-run setup code: ${this.state.setupCode}`)
    }
  }

  /** Mock-mode affordance: the real launcher writes this to data/run/setup-code.txt (TDD.md §9.1). */
  get setupCode(): string {
    return this.state.setupCode
  }

  reset(): void {
    this.state = initialState()
    seedEmails(this)
    this.save()
  }

  private load(): MockState | null {
    if (!this.persist || typeof localStorage === 'undefined') return null
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      return raw ? (JSON.parse(raw) as MockState) : null
    } catch {
      return null
    }
  }

  private save(): void {
    if (this.persist && typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, JSON.stringify(this.state))
  }

  private async done<T>(value: T): Promise<T> {
    this.save()
    if (this.latencyMs) await new Promise((r) => setTimeout(r, this.latencyMs))
    return structuredClone(value)
  }

  private audit(summary: string, candidateId?: number, meta: { action?: string; entity?: string; details?: string } = {}): void {
    const u = this.state.users.find((x) => x.userId === this.state.sessionUserId)
    this.state.audit.unshift({ at: iso(), summary, candidateId, user: u?.fullName ?? 'System', role: u?.role ?? 'System', ...meta })
  }

  /** Saved OCC admin settings (§16.14); older persisted states fall back to defaults. */
  private policy(): AdminSettings {
    return this.state.adminSettings ?? ADMIN_SETTINGS
  }

  private minPasswordLength(role: Role): number {
    const p = this.policy()
    return role === 'Admin' ? p.adminMinPasswordLength : p.analystMinPasswordLength
  }

  private nextId(): number {
    this.state.nextId += 1
    return this.state.nextId
  }

  private currentUser(): MockUser {
    const u = this.state.users.find((x) => x.userId === this.state.sessionUserId)
    if (!u) fail(401, 'Not signed in', 'UNAUTHENTICATED')
    return u
  }

  private requireAdmin(): MockUser {
    const u = this.currentUser()
    if (u.role !== 'Admin') fail(403, "You don't have access to this area", 'FORBIDDEN')
    return u
  }

  private toCurrentUser(u: MockUser): CurrentUser {
    return {
      userId: u.userId,
      fullName: u.fullName,
      email: u.email,
      role: u.role,
      persona: u.persona,
      mustChangePassword: u.mustChangePassword,
      csrfToken: 'mock-csrf',
    }
  }

  private publicUser(u: MockUser): OccUser {
    const { userId, fullName, email, role, persona, status, lastSignIn, passwordSetAt } = u
    return { userId, fullName, email: maskEmail(email) ?? '', role, persona, status, lastSignIn, passwordSetAt }
  }

  activeTemplate(): ScoringTemplate {
    return this.state.templates.find((t) => t.status === 'active') ?? DEFAULT_TEMPLATE
  }

  resources(): Resource[] {
    const { raws, statuses, statusMap, domains } = this.state
    return raws.map((r) => ({ ...toResource(r, statuses, statusMap, domains), candidateType: this.classification(r.resourceId).effectiveClassification }))
  }

  computeResults(): MatchResult[] {
    const demands = this.state.demands.map((d) => ({ ...d, matches: 0, bestBand: null }))
    const results = scoreAll(demands, this.state.raws, this.resources(), this.activeTemplate(), 1, this.policy().emailsPreselected)
    return results.map((r) => {
      const o = this.state.overrides[r.resultId]
      return o ? { ...r, override: o } : r
    })
  }

  private demandsView(): Demand[] {
    const results = this.computeResults()
    return this.state.demands.map((d) => {
      const rows = results.filter((r) => r.demandId === d.demandId)
      return { ...d, matches: rows.length, bestBand: rows[0]?.band ?? null }
    })
  }

  private lastRun: { mode: 'incremental' | 'full'; reason: string } = { mode: 'full', reason: 'requested_full' }

  private run(): MatchRun {
    const results = this.computeResults()
    const scored = this.state.demands.filter((d) => d.jdStatus === 'Confirmed')
    const without = scored.filter((d) => !results.some((r) => r.demandId === d.demandId && r.band !== 'Weak')).length
    return {
      runId: 1,
      status: 'completed',
      finishedAt: '2026-11-04T14:32:00Z',
      llmModel: this.state.models.find((m) => m.id === this.state.settings.activeModel)?.name ?? 'local model',
      templateVersion: this.activeTemplate().version,
      demandsAssessed: scored.length,
      pairsScored: results.length,
      rolesWithoutCandidate: without,
      dataChangedSinceRun: (this.state.dataChanges ?? []).length > 0,
      dataChangedReasons: [...(this.state.dataChanges ?? [])],
      scope: 'full',
      demandIds: [],
      mode: this.lastRun.mode,
      modeReason: this.lastRun.reason,
    }
  }

  async startMatchRun(req: MatchRunRequest) {
    this.currentUser()
    // Mirrors the backend: only a full-scope run can be incremental.
    this.lastRun = req.incremental && req.scope === 'full' ? { mode: 'incremental', reason: 'inputs_compared' } : { mode: 'full', reason: req.incremental ? 'scope_not_full' : 'requested_full' }
    this.state.dataChanges = []
    return this.done({ runId: 1, jobId: 1 })
  }

  private noteDataChange(reason: string): void {
    const changes = (this.state.dataChanges ??= [])
    if (!changes.includes(reason)) changes.push(reason)
  }

  draftFor(r: MatchResult): EmailDraft {
    const s = this.state
    const gaps = r.gapsChecks.filter((g) => g !== 'None')
    const body = s.settings.emailTemplate
      .replaceAll('{candidate_name}', r.personName?.split(' ')[0] ?? 'there')
      .replaceAll('{requisition}', r.codeRequisition)
      .replaceAll('{role}', r.demandRequestName ?? '')
      .replaceAll('{match_pct}', formatPct(r.score))
      .replaceAll('{gaps}', gaps.length ? gaps.map((g) => `• ${g}`).join('\n') : '• None noted')
      .replaceAll('{pointers}', `• Highlight ${r.matchingEvidence.slice(0, 2).join(', ') || 'relevant experience'}`)
      .replaceAll('{spoc_1}', s.settings.spoc1)
      .replaceAll('{spoc_2}', s.settings.spoc2)
    const demand = s.demands.find((d) => d.demandId === r.demandId)
    const prior = s.emails.find((e) => e.resultId === r.resultId && e.status === 'Sent')
    this.state.nextId += 1
    return {
      emailId: this.state.nextId,
      resultId: r.resultId,
      demandId: r.demandId,
      codeRequisition: r.codeRequisition,
      accountName: r.accountName,
      demandRequestName: r.demandRequestName,
      candidateName: r.personName,
      rank: r.rank,
      score: r.score,
      band: r.band,
      toMasked: r.personEmailMasked,
      cc: s.resourceTeam.filter((m) => m.active).map((m) => m.email),
      subject: `Opportunity: ${r.demandRequestName ?? ''} (${r.codeRequisition})`,
      body,
      status: 'Draft',
      preselected: r.emailPreselected,
      demandUploadedAt: demand?.uploadedAt ?? iso(),
      previouslySentAt: prior?.sentAt,
    }
  }

  // ---- Setup and auth -------------------------------------------------------------------
  async getSetupStatus() {
    return this.done({ adminExists: this.state.users.some((u) => u.role === 'Admin') })
  }

  async setupAdmin(req: { setupCode: string; fullName: string; email: string; password: string }) {
    if (this.state.users.some((u) => u.role === 'Admin')) fail(409, 'An administrator already exists', 'SETUP_DONE')
    if (req.setupCode.trim().toUpperCase() !== this.state.setupCode) fail(400, 'The setup code is not valid', 'SETUP_CODE_INVALID')
    if (!checkPassword(req.password, 'Admin', { email: req.email, name: req.fullName }).ok || req.password.length < this.minPasswordLength('Admin'))
      fail(422, 'Password does not meet the administrator policy', 'PASSWORD_POLICY')
    this.state.users.push({
      userId: this.nextId(),
      fullName: req.fullName.trim(),
      email: req.email.trim(),
      role: 'Admin',
      status: 'Active',
      lastSignIn: null,
      passwordSetAt: iso(),
      passwordHash: mockHash(req.password),
      mustChangePassword: false,
      failures: 0,
      lockedUntil: null,
    })
    this.audit('First-run administrator created')
    return this.done(undefined)
  }

  async login(req: { role: Role; email: string; password: string }) {
    const user = this.state.users.find((u) => u.email.trim().toLowerCase() === req.email.trim().toLowerCase())
    if (user?.lockedUntil && user.lockedUntil > Date.now()) {
      fail(423, 'Too many attempts', 'ACCOUNT_LOCKED', { retryAfterSeconds: Math.ceil((user.lockedUntil - Date.now()) / 1000) })
    }
    if (user && user.lockedUntil && user.lockedUntil <= Date.now()) {
      user.lockedUntil = null
      user.failures = 0
      user.status = 'Active'
    }
    const valid = user && user.passwordHash === mockHash(req.password) && user.role === req.role
    if (!valid) {
      if (user) {
        user.failures += 1
        const { lockoutAttempts, lockoutMinutes } = this.policy()
        if (user.failures >= lockoutAttempts) {
          user.lockedUntil = Date.now() + lockoutMinutes * 60_000
          user.status = 'Locked'
          this.audit(`Account locked after ${lockoutAttempts} failed sign-ins`)
          this.save()
          fail(423, 'Too many attempts', 'ACCOUNT_LOCKED', { retryAfterSeconds: lockoutMinutes * 60 })
        }
      }
      this.audit('Sign-in failed')
      this.save()
      fail(401, 'Email, password or role is incorrect.', 'INVALID_CREDENTIALS')
    }
    if (user.status === 'Disabled') fail(403, 'This account is disabled. Contact your PFC administrator.', 'ACCOUNT_DISABLED')
    user.failures = 0
    user.lastSignIn = iso()
    this.state.sessionUserId = user.userId
    this.audit(`Signed in as ${user.role}`)
    return this.done(this.toCurrentUser(user))
  }

  async logout() {
    this.state.sessionUserId = null
    return this.done(undefined)
  }

  async me() {
    const u = this.state.users.find((x) => x.userId === this.state.sessionUserId)
    return this.done(u ? this.toCurrentUser(u) : null)
  }

  async changePassword(req: { currentPassword: string; newPassword: string }) {
    const u = this.currentUser()
    if (u.passwordHash !== mockHash(req.currentPassword)) fail(400, 'Current password is incorrect', 'PASSWORD_MISMATCH')
    if (!checkPassword(req.newPassword, u.role, { email: u.email, name: u.fullName }).ok)
      fail(422, 'Password does not meet the policy', 'PASSWORD_POLICY')
    const min = this.minPasswordLength(u.role)
    if (req.newPassword.length < min) fail(422, `Password must be at least ${min} characters (OCC policy)`, 'PASSWORD_POLICY')
    if (req.newPassword === req.currentPassword) fail(422, 'Choose a different password', 'PASSWORD_REUSED')
    u.passwordHash = mockHash(req.newPassword)
    u.mustChangePassword = false
    u.passwordSetAt = iso()
    this.audit('Password changed')
    return this.done(this.toCurrentUser(u))
  }

  // ---- Demands and ingestion ------------------------------------------------------------
  async listDemands() {
    this.currentUser()
    return this.done(this.demandsView())
  }

  async analyzeDemands(ids: number[]) {
    this.currentUser()
    for (const d of this.state.demands) {
      if (ids.includes(d.demandId) && needsAnalysis(d)) d.jdStatus = 'Draft'
    }
    return this.done(undefined)
  }

  async analyzeDemand(demandId: number) {
    await this.analyzeDemands([demandId])
    return this.getRequirements(demandId)
  }

  async getRequirements(demandId: number) {
    this.currentUser()
    const d = this.state.demands.find((x) => x.demandId === demandId)
    if (!d) fail(404, 'Demand not found', 'NOT_FOUND')
    const saved = this.state.requirements[demandId]
    if (!saved && needsAnalysis(d)) fail(404, 'Requirements not found', 'REQUIREMENTS_NOT_FOUND')
    return this.done(
      saved ?? {
        demandId,
        mandatorySkills: d.mandatorySkills,
        preferredSkills: d.preferredSkills,
        items: [
          ...d.mandatorySkills.map((text, index) => ({ kind: 'mandatory_skill' as const, text, evidence: null, evidenceUnverified: demandId !== 2 && index === 0, classificationReason: demandId !== 2 && index === 0 ? 'classification_adjusted' as const : null })),
          ...d.preferredSkills.map((text, index) => ({ kind: 'preferred_skill' as const, text, evidence: null, evidenceUnverified: false, classificationReason: demandId !== 2 && index === 0 ? 'classification_adjusted' as const : null })),
        ],
        experienceYears: 5,
        qualifications: '',
        domain: d.bu === 'INS' ? 'Insurance' : d.bu === 'BCM' ? 'Banking' : '',
        location: d.region ?? '',
        mandatoryOnsite: false,
        synonymsApplied: d.mandatorySkills.includes('dbt') ? ['dbt ↔ data build tool'] : [],
        warnings: d.preferredSkills.includes('Python') ? ['"Python" mentioned once — confirm priority'] : [],
        // Demand 2 shows the rule-based fallback the backend uses when the local model is unavailable.
        extractionMethod: demandId === 2 ? 'rule_based_fallback' : 'llm',
        fallbackReason: demandId === 2 ? 'provider_unavailable' : null,
      },
    )
  }

  async saveRequirements(req: JdRequirements) {
    this.currentUser()
    this.state.requirements[req.demandId] = req
    const d = this.state.demands.find((x) => x.demandId === req.demandId)
    if (d) {
      d.mandatorySkills = req.mandatorySkills
      d.preferredSkills = req.preferredSkills
      d.jdStatus = 'Draft'
    }
    return this.done(req)
  }

  async confirmRequirements(demandId: number) {
    this.currentUser()
    const d = this.state.demands.find((x) => x.demandId === demandId)
    if (!d) fail(404, 'Demand not found', 'NOT_FOUND')
    d.jdStatus = 'Confirmed'
    this.noteDataChange('JD confirmed')
    this.audit(`JD requirements confirmed for ${d.codeRequisition}; scoring queued`)
    return this.done(undefined)
  }

  async uploadWorkbook(kind: 'dl' | 'ct', file: File) {
    this.currentUser()
    if (!file.name.toLowerCase().endsWith('.xlsx'))
      fail(422, 'Not an .xlsx file. Choose the workbook exported from Excel.', 'INGEST_NOT_XLSX')
    const dl = kind === 'dl'
    return this.done({
      ingestionRunId: this.nextId(),
      kind,
      fileName: file.name,
      sheet: dl ? 'Open Demands Wk45' : 'CT_Front_Sheet',
      rowsRead: dl ? 134 : 61,
      new: dl ? 12 : 4,
      updated: dl ? 21 : 9,
      unchanged: dl ? 98 : 46,
      rejected: dl ? 3 : 2,
      warnings: dl
        ? ['Row 17  Code Requisition "ABCD123" not in T1/T2XXXX format', 'Row 40  JD contains mis-encoded bullets (normalised)']
        : ['Row 9  Bench End Date "Wed 31/13/2026" unparseable — stays eligible, pending review', 'Row 22  Email Address missing'],
      alreadyImported: false,
    })
  }

  async commitIngestion(id: number) {
    this.currentUser()
    this.audit(`Ingestion run ${id} committed`)
    this.noteDataChange('DL/CT committed')
    return this.done(undefined)
  }

  async discardIngestion(id: number) {
    this.currentUser()
    this.audit(`Ingestion run ${id} discarded`)
    return this.done(undefined)
  }

  // ---- Resources and candidates ---------------------------------------------------------
  async listResources() {
    this.currentUser()
    return this.done(this.resources())
  }

  async listAllResources() {
    return this.listResources()
  }

  async listExcludedResources() {
    this.currentUser()
    return this.done(this.resources().filter((r) => r.eligibility === 'excluded' || r.eligibility === 'insufficient'))
  }

  async syncCvs(ids: number[]) {
    this.currentUser()
    if (ids.length < 1) fail(422, 'Select at least one candidate', 'SYNC_EMPTY')
    this.noteDataChange('CV synced')
    for (const r of this.state.raws) {
      if (ids.includes(r.resourceId) && r.cvStatus !== 'Not found') {
        this.state.removedExtractions = this.state.removedExtractions?.filter((id) => Math.floor(id / 10) !== r.resourceId)
        r.cvStatus = 'Synced'
        r.cvCount = Math.max(1, r.cvCount)
      }
    }
    this.audit(`CV sync for ${ids.length} candidate(s)`)
    return this.done(undefined)
  }

  async getCandidate(candidateId: number) {
    this.currentUser()
    const raw = this.state.raws.find((r) => r.resourceId === candidateId)
    const resource = this.resources().find((r) => r.candidateId === candidateId)
    if (!raw || !resource) fail(404, 'Candidate not found', 'NOT_FOUND')
    const surname = (raw.name ?? '').split(' ').pop()?.toLowerCase() ?? ''
    const ctEmailOptions = this.state.raws
      .filter((r) => r.resourceId !== raw.resourceId && r.email && (r.name ?? '').toLowerCase().endsWith(surname))
      .map((r) => ({ resourceId: r.resourceId, label: `${r.name} · ${r.shortName} · ${r.grade ?? '—'}`, emailMasked: maskEmail(r.email) ?? '' }))
    const detail: CandidateDetail = {
      candidateId,
      internalRef: `PFC-C-${String(candidateId).padStart(6, '0')}`,
      classification: this.classification(candidateId),
      resource,
      phoneMasked: maskPhone(raw.phone),
      identityStatus: raw.identityStatus,
      ctEmailOptions,
      cvVersions: Array.from({ length: raw.cvCount }, (_, i) => ({
        cvVersionId: candidateId * 10 + i,
        fileName: `${raw.shortName}_CV_v${raw.cvCount - i}.docx`,
        folderPath: this.state.settings.resumeFolderPath,
        modifiedAt: `2026-10-${String(20 - i * 5).padStart(2, '0')}`,
        extractedAt: '2026-11-04T09:30:00Z',
        isBest: i === 0,
        // Only the latest version has extracted facts; older versions predate fact extraction. CV fact phones use the backend's `***` + last-two mask.
        facts: i === 0 && !this.state.removedExtractions?.includes(candidateId * 10 + i) ? mockCvFacts(raw.shortName, raw.skills, raw.phone ? `***${raw.phone.slice(-2)}` : null) : null,
      })),
      matches: this.computeResults().filter((r) => r.candidateId === candidateId),
      activity: this.state.audit.filter((a) => a.candidateId === candidateId).slice(0, 20),
    }
    return this.done(detail)
  }


  private classification(candidateId: number): CandidateClassification {
    const raw = this.state.raws.find((r) => r.resourceId === candidateId)
    if (!raw) fail(404, 'Candidate not found', 'NOT_FOUND')
    const computed = classify(raw.email, this.state.domains)
    const override = this.state.classificationOverrides?.[candidateId]
    return {
      candidateId, computedClassification: computed,
      effectiveClassification: override?.classificationOverride ?? computed,
      classificationOverride: override?.classificationOverride ?? null,
      overrideReason: override?.overrideReason ?? null,
      overrideBy: override?.overrideBy ?? null,
      overrideAt: override?.overrideAt ?? null,
    }
  }

  async setClassification(candidateId: number, body: { classification: CandidateType | null; reason: string }) {
    const user = this.requireAdmin()
    const current = this.classification(candidateId)
    if (!body.reason.trim() || (body.classification !== null && !['Internal', 'External', 'Unknown'].includes(body.classification))) fail(422, 'A valid classification and reason are required', 'VALIDATION_ERROR')
    const value: CandidateClassification = {
      ...current, classificationOverride: body.classification,
      effectiveClassification: body.classification ?? current.computedClassification,
      overrideReason: body.classification === null ? null : body.reason.trim().replace(/[\w.+-]+@[\w.-]+/g, '[REDACTED]'),
      overrideBy: body.classification === null ? null : user.userId,
      overrideAt: body.classification === null ? null : iso(),
    }
    this.state.classificationOverrides ??= {}
    this.state.classificationOverrides[candidateId] = value
    this.audit('Classification override updated', candidateId)
    this.save()
    return this.done(value)
  }

  async listCvExtractions(candidateId: number) {
    this.requireAdmin()
    const detail = await this.getCandidate(candidateId)
    return detail.cvVersions.map((v, i) => ({
      cvVersionId: v.cvVersionId, fileName: v.fileName,
      versionNo: detail.cvVersions.length - i, extractedAt: v.extractedAt,
      removed: this.state.removedExtractions?.includes(v.cvVersionId) ?? false,
    }))
  }

  private async clearExtractions(candidateId?: number) {
    this.requireAdmin()
    const ids = candidateId === undefined ? this.state.raws.map((r) => r.resourceId) : [candidateId]
    const versions = (await Promise.all(ids.map((id) => this.listCvExtractions(id)))).flat().filter((v) => !v.removed)
    this.state.removedExtractions = [...(this.state.removedExtractions ?? []), ...versions.map((v) => v.cvVersionId)]
    this.audit('CV extractions removed', candidateId)
    this.save()
    return this.done({ versionsCleared: versions.length, chunksDeleted: versions.length, vectorsDeleted: versions.length, matchingVectorsDeleted: versions.length })
  }

  async clearCandidateExtractions(candidateId: number) { return this.clearExtractions(candidateId) }
  async clearAllExtractions() { return this.clearExtractions() }

  async resolveIdentity(candidateId: number, resolution: { action: string; resourceId?: number }) {
    this.currentUser()
    const raw = this.state.raws.find((r) => r.resourceId === candidateId)
    if (!raw) fail(404, 'Candidate not found', 'NOT_FOUND')
    if (resolution.action === 'select_ct_record') {
      const source = this.state.raws.find((r) => r.resourceId === resolution.resourceId)
      // Emails are copied from an existing CT record, never typed or constructed (REQ-IN-14, GQ-06).
      if (!source?.email) fail(422, 'Select a Candidate Tracker record with an email', 'IDENTITY_EMAIL_REQUIRED')
      raw.email = source.email
      raw.name = source.name
      raw.identityStatus = 'complete'
    } else if (resolution.action === 'generate_internal_id') {
      raw.identityStatus = 'incomplete'
    } else {
      raw.identityStatus = 'review_later'
    }
    this.audit(`Identity resolution: ${resolution.action}`, candidateId)
    this.save()
    return this.getCandidate(candidateId)
  }

  // ---- Matching -------------------------------------------------------------------------
  async listMatchRuns() {
    this.currentUser()
    return this.done([this.run()])
  }

  async getMatchRun(runId: number) {
    this.currentUser()
    if (runId !== 1) fail(404, 'Run not found', 'NOT_FOUND')
    return this.done(this.run())
  }

  async getResults(runId: number) {
    this.currentUser()
    if (runId !== 1) fail(404, 'Run not found', 'NOT_FOUND')
    return this.done(this.computeResults())
  }

  async getRunExcluded(runId: number) {
    this.currentUser()
    if (runId !== 1) fail(404, 'Run not found', 'NOT_FOUND')
    return this.done(
      this.resources()
        .filter((r) => r.eligibility === 'excluded' || r.eligibility === 'insufficient')
        .flatMap((r) =>
          r.exclusionReasons.map((detail) => ({
            runId,
            resourceId: r.resourceId,
            shortName: r.shortName,
            demandId: null,
            codeRequisition: null,
            reasonCode: 'EXCLUDED',
            detail,
            scope: 'global' as const,
          })),
        ),
    )
  }

  async addOverride(resultId: number, o: Omit<MatchOverride, 'createdBy' | 'createdAt'>) {
    const u = this.currentUser()
    if (!o.reason.trim()) fail(422, 'A reason is required', 'OVERRIDE_REASON_REQUIRED')
    this.state.overrides[resultId] = { ...o, createdBy: u.fullName, createdAt: iso() }
    const result = this.computeResults().find((r) => r.resultId === resultId)
    if (!result) fail(404, 'Result not found', 'NOT_FOUND')
    this.audit(`Override ${o.action} on ${result.codeRequisition}: ${o.reason}`, result.candidateId)
    return this.done(result)
  }

  async unmask(entity: 'resource' | 'candidate' | 'occ_user', id: number, field: 'email' | 'phone') {
    const me = this.currentUser()
    if (me.role === 'Analyst' && !this.policy().analystsMayUnmask) fail(403, 'Unmasking is disabled for Analysts', 'UNMASK_DISABLED')
    let value: string | null | undefined
    if (entity === 'occ_user') this.requireAdmin()
    if (entity === 'occ_user') value = this.state.users.find((u) => u.userId === id)?.email
    else {
      const raw = this.state.raws.find((r) => r.resourceId === id)
      value = field === 'email' ? raw?.email : raw?.phone
    }
    if (!value) fail(404, 'Not recorded', 'NOT_FOUND')
    this.audit(`Unmasked ${field}`, entity === 'occ_user' ? undefined : id)
    return this.done(value)
  }

  // ---- Analytics ------------------------------------------------------------------------
  private filtered(f: DashboardFilters) {
    const has = (a: string | null | undefined, b?: string) => !b || (a ?? '').toLowerCase() === b.toLowerCase()
    const demands = this.demandsView().filter(
      (d) =>
        (!f.codeRequisition || d.codeRequisition.toLowerCase().includes(f.codeRequisition.toLowerCase())) &&
        has(d.accountName, f.accountName) &&
        has(d.bu, f.bu) &&
        has(d.codeLocalGrade, f.grade) &&
        has(d.region, f.region),
    )
    const resources = this.resources().filter(
      (r) =>
        has(r.grade, f.grade) &&
        has(r.location, f.location) &&
        has(r.benchStatus, f.status) &&
        (!f.candidateType || r.candidateType === f.candidateType) &&
        (!f.incompleteIdentity || (r.identityStatus !== 'complete') === (f.incompleteIdentity === 'yes')),
    )
    const rf: ResultFilters = { ...f, demandGrade: undefined, personGrade: undefined }
    const ids = new Set(demands.map((d) => d.demandId))
    const results = applyResultFilters(this.computeResults(), rf).filter(
      (r) => ids.has(r.demandId) && (!f.grade || r.demandGrade === f.grade || r.personGrade === f.grade),
    )
    return { demands, resources, results }
  }

  private validation(results: MatchResult[], resources: Resource[]): ValidationCheck[] {
    const byReq = new Map<string, number>()
    results.forEach((r) => byReq.set(r.codeRequisition, (byReq.get(r.codeRequisition) ?? 0) + 1))
    const filledEnd = new Set(resources.filter((r) => r.benchEndDate).map((r) => r.resourceId))
    return [
      { id: 'bench-end', label: 'Valid filled Bench End Date excluded', passed: !results.some((r) => filledEnd.has(r.resourceId)) },
      { id: 'grade', label: '±1 grade rule applied', passed: results.every((r) => r.gradeRelation) },
      { id: 'top5', label: 'At most five candidates per requisition', passed: [...byReq.values()].every((n) => n <= 5) },
      { id: 'email-copy', label: 'Emails copied exactly from the CT', passed: true },
      { id: 'masked', label: 'Masked email shown after name', passed: true },
      { id: 'evidence', label: 'Evidence and gaps on every row', passed: results.every((r) => r.matchingEvidence.length > 0 && r.gapsChecks.length > 0), detail: 'Rows without matching evidence need review' },
      { id: 'capacity', label: 'Available Capacity not used for availability', passed: true },
      { id: 'unparseable', label: 'Unparseable dates stay eligible (pending review)', passed: true },
    ]
  }

  async exportResults(runId: number, f: ResultFilters, format: 'csv' | 'xlsx', includeEmails: boolean) {
    const me = this.currentUser()
    if (includeEmails && me.role === 'Analyst' && !this.policy().analystsMayUnmask) fail(403, 'Unmasking is disabled for Analysts', 'UNMASK_DISABLED')
    if (format === 'xlsx') fail(501, 'XLSX export needs the backend', 'NOT_IMPLEMENTED')
    const rows0 = applyResultFilters(await this.getResults(runId), f)
    const details = `run ${runId} · ${format} · rows ${rows0.length}`
    if (includeEmails) this.audit('Unmasked emails in results export', undefined, { action: 'unmask', entity: 'match_run', details })
    this.audit(`Exported results for run ${runId}`, undefined, { action: 'export_results', entity: 'match_run', details })
    const raw = new Map(this.state.raws.map((r) => [r.resourceId, r.email]))
    const rows = rows0.map((r) =>
      includeEmails ? { ...r, personEmailMasked: raw.get(r.resourceId) ?? r.personEmailMasked } : r,
    )
    this.save()
    return { blob: new Blob([resultsToCsv(rows, `Run ${runId}`)], { type: 'text/csv;charset=utf-8' }), fileName: `PFC-results-run${runId}.csv` }
  }

  async analyticsSummary(f: DashboardFilters) {
    this.currentUser()
    const { demands, resources, results } = this.filtered(f)
    return this.done(summarise(demands, resources, results, this.state.emails, this.run()))
  }

  async analyticsDemand(f: DashboardFilters) {
    this.currentUser()
    return this.done(demandAnalytics(this.filtered(f).demands))
  }

  async analyticsBench(f: DashboardFilters) {
    this.currentUser()
    const { demands, resources } = this.filtered(f)
    return this.done(benchAnalytics(resources, demands, new Date()))
  }

  async analyticsMatch(f: DashboardFilters) {
    this.currentUser()
    const { demands, resources, results } = this.filtered(f)
    const emails = this.state.emails.filter((e) => demands.some((d) => d.demandId === e.demandId))
    const summary = summarise(demands, resources, results, emails, this.run())
    return this.done(matchAnalytics(summary, results, emails, this.validation(results, resources)))
  }

  // ---- Email ----------------------------------------------------------------------------
  async createDrafts(resultIds: number[]) {
    this.currentUser()
    const results = this.computeResults().filter((r) => resultIds.includes(r.resultId))
    const created: EmailDraft[] = []
    for (const r of results) {
      if (this.state.emails.some((e) => e.resultId === r.resultId && e.status !== 'Sent')) continue
      const d = this.draftFor(r)
      d.preselected = true
      this.state.emails.push(d)
      created.push(d)
    }
    return this.done(created)
  }

  async listEmails(): Promise<EmailList> {
    this.currentUser()
    return this.done({ items: this.state.emails, transport: { transport: 'Outlook COM', outlookDetected: true } })
  }

  private email(id: number): EmailDraft {
    const e = this.state.emails.find((x) => x.emailId === id)
    if (!e) fail(404, 'Email not found', 'NOT_FOUND')
    return e
  }

  async updateEmail(id: number, patch: Partial<Pick<EmailDraft, 'subject' | 'body' | 'cc' | 'status'>>) {
    this.currentUser()
    const e = this.email(id)
    if (patch.status && !(patch.status === 'Draft' && e.status === 'Approved')) fail(409, 'Only Approved → Draft is allowed here', 'EMAIL_STATE')
    if (e.status === 'Sent') fail(409, 'Sent emails cannot be edited', 'EMAIL_STATE')
    Object.assign(e, patch)
    return this.done(e)
  }

  async approveEmail(id: number, confirmResend?: boolean) {
    this.currentUser()
    const e = this.email(id)
    if (e.previouslySentAt && !confirmResend)
      fail(409, `Already sent on ${e.previouslySentAt.slice(0, 10)}. Re-send?`, 'EMAIL_DUPLICATE_PAIR')
    if (e.status !== 'Draft') fail(409, 'Only drafts can be approved', 'EMAIL_STATE')
    e.status = 'Approved'
    this.audit(`Email approved for ${e.codeRequisition}`, this.computeResults().find((r) => r.resultId === e.resultId)?.candidateId)
    return this.done(e)
  }

  async sendEmail(id: number) {
    this.currentUser()
    const e = this.email(id)
    if (e.status !== 'Approved' && e.status !== 'Failed') fail(409, 'Approve the email before sending', 'EMAIL_NOT_APPROVED')
    e.status = 'Sent'
    e.sentAt = iso()
    e.failureReason = undefined
    this.audit(`Email sent for ${e.codeRequisition}`)
    return this.done(e)
  }

  // ---- Settings -------------------------------------------------------------------------
  async getMySettings() {
    this.currentUser()
    return this.done(this.state.settings)
  }

  async saveMySettings(s: PersonalSettings) {
    this.currentUser()
    const model = this.state.models.find((m) => m.id === s.activeModel)
    if (!model?.approved) fail(422, 'Choose a model from the approved list', 'MODEL_NOT_APPROVED')
    this.state.settings = { ...s, autoSyncDl: { ...s.autoSyncDl, enabled: false }, autoSyncCt: { ...s.autoSyncCt, enabled: false } }
    this.audit('Personal settings saved')
    return this.done(this.state.settings)
  }

  async listModels() {
    this.currentUser()
    return this.done(this.state.models)
  }

  // ---- OCC: DL/CT approvals (§16.4), grades (§16.8), models (§16.10), audit (§16.12), admin settings (§16.14)

  async listApprovals() {
    this.requireAdmin()
    return this.done(this.state.approvals)
  }

  async decideApproval(entry: ApprovalEntry, d: ApprovalDecision) {
    this.requireAdmin()
    const e = this.state.approvals.find((x) => x.entryId === entry.entryId)
    if (!e) fail(404, 'Entry not found', 'NOT_FOUND')
    if (e.state !== 'Pending') fail(409, 'This entry has already been decided', 'ALREADY_DECIDED')
    const comment = d.comment?.trim()
    if ((d.action === 'modify' || d.action === 'revert') && !comment) fail(422, 'A reason is required', 'REASON_REQUIRED')
    this.noteDataChange('OCC approval decision')
    if (d.action === 'modify') {
      const values = d.values ?? {}
      if (!Object.keys(values).length) fail(422, 'Enter at least one corrected value', 'NO_CHANGES')
      e.fields = e.fields.map((f) => (f.field in values ? { ...f, after: values[f.field] } : f))
    }
    e.state = d.action === 'approve' ? 'Approved' : d.action === 'modify' ? 'Modified' : 'Reverted'
    e.comment = comment
    this.audit(`${e.source} ${e.key}: ${e.state}${comment ? ` — ${comment}` : ''}`, undefined, { action: d.action, entity: e.source === 'DL' ? 'demand' : 'resource', details: JSON.stringify(e.fields) })
    return this.done(e)
  }

  async listGrades() {
    this.requireAdmin()
    return this.done(this.state.grades)
  }

  async saveGrades(grades: Grade[]) {
    this.requireAdmin()
    const codes = grades.map((g) => g.code.trim().toUpperCase())
    if (codes.some((c) => !/^[A-Z]\d$/.test(c))) fail(422, 'Grade codes must be a letter followed by a digit (e.g. C2)', 'GRADE_FORMAT')
    if (new Set(codes).size !== codes.length) fail(422, 'Grade codes must be unique', 'GRADE_DUPLICATE')
    if (!grades.some((g) => !g.retired)) fail(422, 'At least one active grade is required', 'GRADE_EMPTY')
    this.state.grades = grades.map((g, i) => ({ code: codes[i], order: i + 1, retired: g.retired }))
    this.audit(`Grade list saved: ${this.state.grades.filter((g) => !g.retired).map((g) => g.code).join(' < ')}`, undefined, { action: 'update', entity: 'grade_list' })
    return this.done(this.state.grades)
  }

  async listOccModels() {
    this.requireAdmin()
    return this.done(this.state.models)
  }

  async updateOccModel(id: string, patch: Partial<Pick<OccLlmModel, 'approved' | 'isDefault'>>) {
    this.requireAdmin()
    const m = this.state.models.find((x) => x.id === id)
    if (!m) fail(404, 'Model not found', 'NOT_FOUND')
    if (patch.approved === false && m.isDefault) fail(409, 'Choose another default before revoking approval', 'MODEL_IS_DEFAULT')
    if (patch.isDefault && !(patch.approved ?? m.approved)) fail(422, 'Only an approved model can be the default', 'MODEL_NOT_APPROVED')
    if (patch.approved !== undefined) m.approved = patch.approved
    if (patch.isDefault) for (const x of this.state.models) if (x.purpose === m.purpose) x.isDefault = x.id === m.id
    this.audit(`Model ${m.name}: ${patch.isDefault ? 'set as default' : m.approved ? 'approved' : 'approval revoked'}`, undefined, { action: 'update', entity: 'llm_model' })
    return this.done(this.state.models)
  }

  async auditLog(q: AuditQuery) {
    this.requireAdmin()
    const user = q.user?.trim().toLowerCase()
    const rows: AuditEvent[] = this.state.audit
      .map((a, i) => ({
        auditId: this.state.audit.length - i,
        at: a.at,
        user: a.user ?? 'System',
        role: a.role ?? 'System',
        action: a.action ?? 'event',
        entity: a.entity ?? (a.candidateId ? 'candidate' : 'system'),
        summary: a.summary,
        detailsJson: a.details,
      }))
      .filter((a) => (!q.from || a.at.slice(0, 10) >= q.from) && (!q.to || a.at.slice(0, 10) <= q.to))
      .filter((a) => (!user || a.user.toLowerCase().includes(user)) && (!q.action || a.action === q.action) && (!q.entity || a.entity === q.entity))
    return this.done(rows)
  }

  async getAdminSettings() {
    this.requireAdmin()
    return this.done(this.state.adminSettings)
  }

  async getHealth() {
    return this.done({ status: 'ok', dataVolumeEncryption: 'Unknown' as const })
  }

  async saveAdminSettings(next: AdminSettings) {
    this.requireAdmin()
    const errors: string[] = []
    if (next.analystMinPasswordLength < 10) errors.push('Analyst minimum password length cannot be below 10 (TDD §9.2)')
    if (next.adminMinPasswordLength < 14) errors.push('Admin minimum password length cannot be below 14 (TDD §9.2)')
    if (next.lockoutAttempts < 3 || next.lockoutAttempts > 10) errors.push('Lockout attempts must be 3–10')
    if (next.lockoutMinutes < 5 || next.lockoutMinutes > 120) errors.push('Lockout duration must be 5–120 minutes')
    if (next.idleTimeoutMinutes < 5 || next.idleTimeoutMinutes > 480) errors.push('Idle timeout must be 5–480 minutes')
    if (next.maskKeep !== 2 || next.maskCount !== 6) errors.push('Masking rule is fixed at keep 2 / mask 6 (REQ-OUT-03)')
    if (next.topN !== 5) errors.push('Shortlist size is fixed at 5 per requisition (REQ-MAT-03)')
    if (next.emailsPreselected < 1 || next.emailsPreselected > next.topN) errors.push(`Pre-selected emails must be 1–${next.topN}`)
    if (next.allowExternalProviders) errors.push('External providers are configuration placeholders until Phase 3 (REQ-LLM-03)')
    if (errors.length) fail(422, errors.join('; '), 'VALIDATION')
    const before = this.state.adminSettings
    const changed = (Object.keys(next) as (keyof AdminSettings)[]).filter((k) => next[k] !== before[k])
    this.state.adminSettings = { ...next }
    // A changed pre-select count resets the default selection on pending drafts (REQ-EML-01).
    if (before.emailsPreselected !== next.emailsPreselected)
      for (const e of this.state.emails) if (e.status === 'Draft' && e.rank) e.preselected = e.rank <= next.emailsPreselected
    this.audit(`Admin settings saved (${changed.join(', ') || 'no changes'})`, undefined, { action: 'update', entity: 'admin_settings', details: JSON.stringify(Object.fromEntries(changed.map((k) => [k, { from: before[k], to: next[k] }]))) })
    return this.done(this.state.adminSettings)
  }

  async previewPackage(file: File) {
    this.currentUser()
    const errors: string[] = []
    if (!file.name.toLowerCase().endsWith('.pfcpkg')) errors.push('Not a .pfcpkg settings package')
    const version = Number(/v(\d+)/i.exec(file.name)?.[1] ?? 0)
    const signatureValid = !/tampered|unsigned/i.test(file.name)
    if (!signatureValid) errors.push('Signature verification failed — the package was not signed by your OCC Admin')
    const newer = version > this.state.installedPackageVersion
    if (version && !newer) errors.push(`Version v${version} is not newer than installed v${this.state.installedPackageVersion}`)
    if (!version) errors.push('Package version not found in manifest')
    const preview: PackagePreview = {
      previewId: `pv-${this.nextId()}`,
      fileName: file.name,
      packageVersion: version,
      installedVersion: this.state.installedPackageVersion,
      signatureValid,
      schemaCompatible: true,
      appCompatible: true,
      newer,
      importedAccounts: 2,
      errors,
      changes: [
        { area: 'Skill synonyms', change: 'added', item: 'GenAI ↔ Generative AI' },
        { area: 'Scoring template', change: 'changed', item: 'Grade alignment ±1 points', before: '7', after: '6' },
        { area: 'Status mapping', change: 'added', item: 'Bench - awaiting role → Available' },
        { area: 'Resource-team list', change: 'removed', item: 'resourcing.uk@example.com' },
        { area: 'Analyst accounts', change: 'added', item: '2 accounts (one-time password, change required at first sign-in)' },
      ],
    }
    this.state.previews = [preview]
    return this.done(preview)
  }

  async importPackage(source: PackageSource) {
    const u = this.currentUser()
    const p = 'previewId' in source ? this.state.previews.find((x) => x.previewId === source.previewId) : undefined
    if (!p) fail(404, 'Preview expired — choose the file again', 'PACKAGE_PREVIEW_EXPIRED')
    if (p.errors.length) fail(422, `Package not applied: ${p.errors[0]}. Current settings are unchanged.`, 'PACKAGE_INVALID')
    this.state.packageHistory = [
      { version: p.packageVersion, importedAt: iso(), importedBy: u.fullName, fileName: p.fileName, canRollback: true, status: 'applied', isCurrent: true },
      ...this.state.packageHistory.map((h) => ({ ...h, canRollback: false, isCurrent: false })),
    ]
    this.state.installedPackageVersion = p.packageVersion
    this.state.previews = []
    this.audit(`Settings package v${p.packageVersion} imported`)
    return this.done(this.state.packageHistory)
  }

  async rollbackPackage() {
    this.currentUser()
    const h = this.state.packageHistory
    const latest = h.find((x) => x.isCurrent)
    const previous = h.find((x) => x !== latest && x.status === 'applied' && latest && x.version < latest.version)
    if (!latest?.canRollback || !previous) fail(409, 'Nothing to roll back', 'PACKAGE_NO_ROLLBACK')
    this.state.packageHistory = h.map((x) =>
      x === latest ? { ...x, status: 'rolled_back', isCurrent: false, canRollback: false } : x === previous ? { ...x, isCurrent: true, canRollback: h.some((y) => y.status === 'applied' && y.version < previous.version) } : x,
    )
    this.state.installedPackageVersion = previous.version
    this.audit(`Settings package rolled back to v${previous.version}`)
    return this.done(this.state.packageHistory)
  }

  async pendingPackages() {
    this.currentUser()
    return this.done<PendingPackage[]>([])
  }

  async packageHistory() {
    this.currentUser()
    return this.done(this.state.packageHistory)
  }

  // ---- OCC ------------------------------------------------------------------------------
  async listUsers() {
    this.requireAdmin()
    return this.done(this.state.users.map((u) => this.publicUser(u)))
  }

  async createUser(req: { fullName: string; email: string; role: Role; persona?: 'A' | 'B' | 'C' }) {
    this.requireAdmin()
    const email = req.email.trim()
    if (this.state.users.some((u) => u.email.toLowerCase() === email.toLowerCase()))
      fail(409, 'A user with this email already exists', 'USER_DUPLICATE')
    const otp = oneTimePassword()
    const userId = this.nextId()
    this.state.users.push({
      userId,
      fullName: req.fullName.trim(),
      email,
      role: req.role,
      persona: req.persona,
      status: 'Active',
      lastSignIn: null,
      passwordSetAt: iso(),
      passwordHash: mockHash(otp),
      mustChangePassword: true,
      failures: 0,
      lockedUntil: null,
    })
    this.audit(`User added (${req.role})`)
    return this.done({ userId, oneTimePassword: otp })
  }

  async resetPassword(userId: number) {
    this.requireAdmin()
    const u = this.state.users.find((x) => x.userId === userId)
    if (!u) fail(404, 'User not found', 'NOT_FOUND')
    const otp = oneTimePassword()
    u.passwordHash = mockHash(otp)
    u.mustChangePassword = true
    u.failures = 0
    u.lockedUntil = null
    if (u.status === 'Locked') u.status = 'Active'
    this.audit('Password reset by Admin')
    return this.done({ userId, oneTimePassword: otp })
  }

  async unlockUser(userId: number) {
    this.requireAdmin()
    const u = this.state.users.find((x) => x.userId === userId)
    if (!u) fail(404, 'User not found', 'NOT_FOUND')
    u.failures = 0
    u.lockedUntil = null
    u.status = 'Active'
    this.audit('Account unlocked')
    return this.done(this.publicUser(u))
  }

  async updateUser(userId: number, patch: { status?: UserStatus; role?: Role }) {
    const me = this.requireAdmin()
    const u = this.state.users.find((x) => x.userId === userId)
    if (!u) fail(404, 'User not found', 'NOT_FOUND')
    const admins = this.state.users.filter((x) => x.role === 'Admin' && x.status !== 'Disabled')
    const losesAdmin = u.role === 'Admin' && ((patch.role && patch.role !== 'Admin') || patch.status === 'Disabled')
    if (losesAdmin && admins.length <= 1) fail(409, 'You cannot demote or disable the last Admin', 'LAST_ADMIN')
    if (u.userId === me.userId && patch.status === 'Disabled') fail(409, 'You cannot disable your own account', 'SELF_DISABLE')
    Object.assign(u, patch)
    this.audit(`User updated: ${JSON.stringify(patch)}`)
    return this.done(this.publicUser(u))
  }

  async listBenchStatuses() {
    this.requireAdmin()
    return this.done(this.state.statuses)
  }

  /** Per-item like the backend: deleting a status still referenced by the map is a 409. */
  async saveBenchStatuses(statuses: BenchStatus[]) {
    this.requireAdmin()
    const s = this.state
    try {
      await applyChanges(s.statuses, statuses, (x) => x.code, (a, b) => JSON.stringify(a) === JSON.stringify(b), {
        create: async (x) => void (s.statuses = [...s.statuses, x]),
        update: async (x) => void (s.statuses = s.statuses.map((y) => (y.code === x.code ? x : y))),
        remove: async (x) => {
          if (s.statusMap.some((m) => m.canonicalCode === x.code)) fail(409, 'Status is referenced by status_map', 'CONFLICT')
          s.statuses = s.statuses.filter((y) => y.code !== x.code)
        },
      })
      s.statuses = statuses
    } finally {
      this.audit('Status list saved')
      this.save()
    }
    return this.done(s.statuses)
  }

  private statusMapData(): StatusMapData {
    const known = new Set(this.state.statusMap.map((m) => m.sourceText.trim().toLowerCase()))
    const unmapped = [...new Set(this.state.raws.map((r) => r.benchStatus).filter((s): s is string => !!s))].filter(
      (s) => !known.has(s.trim().toLowerCase()),
    )
    return { entries: this.state.statusMap, unmapped }
  }

  async getStatusMap() {
    this.requireAdmin()
    return this.done(this.statusMapData())
  }

  async saveStatusMap(entries: StatusMapEntry[]) {
    this.requireAdmin()
    const s = this.state
    const put = async (e: StatusMapEntry) => {
      if (e.canonicalCode && !s.statuses.some((x) => x.code === e.canonicalCode)) fail(422, 'Unknown canonical status', 'VALIDATION_ERROR')
      s.statusMap = [...s.statusMap.filter((m) => m.sourceText !== e.sourceText), e]
    }
    try {
      await applyChanges(s.statusMap, entries, (e) => e.sourceText, (a, b) => a.canonicalCode === b.canonicalCode, {
        create: put,
        update: put,
        remove: async (e) => void (s.statusMap = s.statusMap.filter((m) => m.sourceText !== e.sourceText)),
      })
    } finally {
      this.audit('Source-status mapping saved')
      this.save()
    }
    return this.done(this.statusMapData())
  }

  async listSynonyms() {
    this.requireAdmin()
    return this.done(this.state.synonyms)
  }

  async createSynonym(s: Omit<Synonym, 'synonymId' | 'usageCount' | 'source' | 'status'>) {
    this.requireAdmin()
    if (this.state.synonyms.some((x) => x.canonical.toLowerCase() === s.canonical.trim().toLowerCase()))
      fail(409, 'Canonical term already exists', 'SYNONYM_DUPLICATE')
    const created: Synonym = { ...s, canonical: s.canonical.trim(), synonymId: this.nextId(), usageCount: 0, source: 'admin', status: 'Active' }
    this.state.synonyms.push(created)
    this.audit(`Synonym added: ${created.canonical}`)
    return this.done(created)
  }

  async updateSynonym(id: number, patch: Partial<Synonym>) {
    this.requireAdmin()
    const s = this.state.synonyms.find((x) => x.synonymId === id)
    if (!s) fail(404, 'Synonym not found', 'NOT_FOUND')
    Object.assign(s, patch)
    this.audit(`Synonym updated: ${s.canonical}`)
    return this.done(s)
  }

  async listTemplates() {
    this.requireAdmin()
    return this.done([...this.state.templates].sort((a, b) => b.version - a.version))
  }

  async publishTemplate(draft: Omit<ScoringTemplate, 'templateId' | 'version' | 'status' | 'createdAt' | 'createdBy'>) {
    const u = this.requireAdmin()
    const total = Object.values(draft.weights).reduce((a, b) => a + b, 0)
    if (total !== 100) fail(422, `Weights must total 100 (currently ${total})`, 'TEMPLATE_WEIGHTS')
    if (!draft.changeNote.trim()) fail(422, 'A change note is required', 'TEMPLATE_NOTE')
    const { strong, potential, avg } = draft.bands
    if (!(strong > potential && potential > avg && avg > 0)) fail(422, 'Band thresholds must be descending', 'TEMPLATE_BANDS')
    const version = Math.max(...this.state.templates.map((t) => t.version)) + 1
    this.state.templates = this.state.templates.map((t) => (t.status === 'active' ? { ...t, status: 'retired' } : t))
    this.state.templates.push({ ...draft, templateId: this.nextId(), version, status: 'active', createdAt: iso(), createdBy: u.fullName })
    this.audit(`Scoring template v${version} published`)
    return this.listTemplates()
  }

  private emailTemplate(id: number): EmailTemplateVersion {
    const t = this.state.emailTemplates.find((x) => x.emailTemplateId === id)
    if (!t) fail(404, 'Email template not found', 'NOT_FOUND')
    return t
  }

  async listEmailTemplates(all: boolean) {
    this.requireAdmin()
    const ts = this.state.emailTemplates
    const latest = (t: EmailTemplateVersion) => t.version === Math.max(...ts.filter((x) => x.name === t.name).map((x) => x.version))
    return this.done((all ? [...ts] : ts.filter(latest)).sort((a, b) => a.name.localeCompare(b.name) || b.version - a.version))
  }

  async getEmailTemplate(id: number) {
    this.requireAdmin()
    return this.done(this.emailTemplate(id))
  }

  async createEmailTemplate(d: EmailTemplateDraft) {
    const u = this.requireAdmin()
    const name = d.name.trim()
    if (!name) fail(422, 'A template name is required', 'VALIDATION_ERROR')
    const check = checkTemplate(d.subject, d.bodyHtml)
    if (check.unknown.length || check.malformed) fail(422, 'Unknown email placeholder', 'UNKNOWN_PLACEHOLDER')
    const version = Math.max(0, ...this.state.emailTemplates.filter((t) => t.name === name).map((t) => t.version)) + 1
    const row: EmailTemplateVersion = { emailTemplateId: this.nextId(), name, version, subject: d.subject, bodyHtml: d.bodyHtml, isDefault: false, createdBy: u.userId, createdAt: iso() }
    this.state.emailTemplates.push(row)
    this.audit(`Email template ${name} v${version} created`, undefined, { action: 'update', entity: 'email_templates' })
    return this.done(row)
  }

  async setDefaultEmailTemplate(id: number) {
    this.requireAdmin()
    const t = this.emailTemplate(id)
    this.state.emailTemplates = this.state.emailTemplates.map((x) => ({ ...x, isDefault: x.emailTemplateId === id }))
    this.audit(`Email template ${t.name} v${t.version} set as default`, undefined, { action: 'update', entity: 'email_templates' })
    return this.done(this.emailTemplate(id))
  }

  async listResourceTeam() {
    this.requireAdmin()
    return this.done(this.state.resourceTeam)
  }

  async saveResourceTeam(members: ResourceTeamMember[]) {
    this.requireAdmin()
    const s = this.state
    const key = (m: ResourceTeamMember) => m.email.toLowerCase()
    try {
      await applyChanges(s.resourceTeam, members, key, (a, b) => a.displayName === b.displayName && a.active === b.active, {
        create: async (m) => void (s.resourceTeam = [...s.resourceTeam, m]),
        update: async (m) => void (s.resourceTeam = s.resourceTeam.map((x) => (key(x) === key(m) ? m : x))),
        remove: async (m) => void (s.resourceTeam = s.resourceTeam.filter((x) => key(x) !== key(m))),
      })
    } finally {
      this.audit('Resource-team list saved')
      this.save()
    }
    return this.done(s.resourceTeam)
  }

  async listInternalDomains() {
    this.requireAdmin()
    return this.done(this.state.domains.map((domain) => ({ domain })))
  }

  async saveInternalDomains(domains: { domain: string }[]) {
    this.requireAdmin()
    const s = this.state
    try {
      await applyChanges(s.domains.map((domain) => ({ domain })), domains, (d) => d.domain, () => true, {
        create: async (d) => void (s.domains = [...s.domains, d.domain]),
        update: async () => undefined,
        remove: async (d) => void (s.domains = s.domains.filter((x) => x !== d.domain)),
      })
    } finally {
      this.audit('Internal email domains saved; classification re-evaluated')
      this.save()
    }
    return this.done(s.domains.map((domain) => ({ domain })))
  }

  async previewClassification(domains: { domain: string }[]) {
    this.requireAdmin()
    const list = domains.map((d) => d.domain)
    const types = this.state.raws.map((r) => classify(r.email, list))
    return this.done({
      internal: types.filter((t) => t === 'Internal').length,
      external: types.filter((t) => t === 'External').length,
      unknown: types.filter((t) => t === 'Unknown').length,
    })
  }

  async lastExportedPackage() {
    this.requireAdmin()
    return this.done(this.state.lastExport)
  }

  async exportPackage(passphrase: string) {
    this.requireAdmin()
    if (!passphrase) fail(422, 'Enter the signing-key passphrase to export', 'PASSPHRASE_REQUIRED')
    const version = Math.max(this.state.lastExport?.version ?? 0, this.state.installedPackageVersion) + 1
    const s = this.state
    const info: SettingsPackageInfo = {
      version,
      fileName: `PFC-settings-v${version}.pfcpkg`,
      createdAt: iso(),
      signed: true,
      contents: [
        { label: 'Skill synonyms', count: s.synonyms.filter((x) => x.status === 'Active').length },
        { label: 'Scoring template (active)', count: 1 },
        { label: 'Bench statuses', count: s.statuses.length },
        { label: 'Status-map entries', count: s.statusMap.length },
        { label: 'Grades', count: 9 },
        { label: 'Resource-team recipients', count: s.resourceTeam.length },
        { label: 'Internal domains', count: s.domains.length },
        { label: 'Approved models', count: this.state.models.filter((m) => m.approved).length },
        { label: 'Analyst accounts', count: s.users.filter((u) => u.role === 'Analyst').length },
      ],
    }
    s.lastExport = info
    this.audit(`Settings package v${version} exported`)
    return this.done({ ...info, contentBase64: btoa(`mock package v${version}`), oneTimePasswords: [] })
  }
}
