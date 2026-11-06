/**
 * Backend (Pydantic, snake_case) ↔ SPA DTO mappers for merged `/api/v1` endpoints (TDD.md §12).
 * Shapes observed from the running backend; SQLite booleans arrive as 0/1.
 */
import type {
  AdminSettings,
  AuditEvent,
  ClassificationPreview,
  CurrentUser,
  EmailDraft,
  Grade,
  InternalDomain,
  LlmModel,
  OccUser,
  PersonalSettings,
  Resource,
  ResourceTeamMember,
  Role,
  Synonym,
} from './types'
import { maskEmail } from '../lib/mask'

type Flag = boolean | 0 | 1
const b = (v: Flag | null | undefined): boolean => Boolean(v)

export interface UserView { user_id: number; email: string; display_name: string; role: string; must_change_password: Flag }
export interface AuthView { user: UserView; role_at_login: string; must_change_password: Flag; csrf_token: string }

export function toCurrentUser(v: AuthView): CurrentUser {
  return {
    userId: v.user.user_id,
    fullName: v.user.display_name,
    email: v.user.email,
    role: v.role_at_login as Role,
    mustChangePassword: b(v.must_change_password),
    csrfToken: v.csrf_token,
  }
}

export interface UserRow extends UserView {
  persona: OccUser['persona'] | null
  status: OccUser['status']
  last_login_at: string | null
  updated_at?: string
}

export const toOccUser = (r: UserRow): OccUser => ({
  userId: r.user_id,
  fullName: r.display_name,
  email: r.email,
  role: r.role as Role,
  persona: r.persona ?? undefined,
  status: r.status,
  lastSignIn: r.last_login_at,
  passwordSetAt: null,
})

export interface GradeRow { code: string; ordinal: number; active: Flag }
export const toGrade = (g: GradeRow): Grade => ({ code: g.code, order: g.ordinal, retired: !b(g.active) })
/** PUT /occ/grades: array order is the grade order. */
export const fromGrades = (gs: Grade[]) =>
  [...gs].sort((x, y) => x.order - y.order).map((g) => ({ code: g.code, active: !g.retired }))

export interface ModelRow {
  model_id: number
  name: string
  file_name: string
  size_mb: number
  ram_estimate_mb: number
  licence: string
  approved: Flag
  is_active: Flag
}
const gb = (mb: number) => Math.round((mb / 1024) * 10) / 10
export const toLlmModel = (m: ModelRow): LlmModel => ({
  id: String(m.model_id),
  name: m.name,
  purpose: /embed/i.test(m.name) ? 'embedding' : 'generation',
  ramGb: gb(m.ram_estimate_mb),
  diskGb: gb(m.size_mb),
  licence: m.licence,
  approved: b(m.approved),
  isDefault: b(m.is_active),
})

export interface PersonalRow {
  resume_root_path: string | null
  watch: { dl: { enabled: boolean; path: string | null; pattern: string }; ct: { enabled: boolean; path: string | null; pattern: string } }
  active_model_id: number | null
  density: 'comfortable' | 'compact'
}
/** Backend-only fields (SPOCs, personal template) are not stored server-side yet; defaults are kept. */
export function toPersonal(r: PersonalRow, fallback: PersonalSettings): PersonalSettings {
  const w = (x: PersonalRow['watch']['dl']) => ({ enabled: x.enabled, watchPath: x.path ?? '', pattern: x.pattern })
  return {
    ...fallback,
    resumeFolderPath: r.resume_root_path ?? '',
    autoSyncDl: w(r.watch.dl),
    autoSyncCt: w(r.watch.ct),
    activeModel: r.active_model_id == null ? '' : String(r.active_model_id),
    density: r.density === 'compact' ? 'Compact' : 'Comfortable',
  }
}
export function fromPersonal(s: PersonalSettings) {
  const w = (x: PersonalSettings['autoSyncDl']) => ({ enabled: x.enabled, path: x.watchPath || null, pattern: x.pattern })
  return {
    resume_root_path: s.resumeFolderPath || null,
    watch: { dl: w(s.autoSyncDl), ct: w(s.autoSyncCt) },
    active_model_id: s.activeModel && /^\d+$/.test(s.activeModel) ? Number(s.activeModel) : null,
    density: s.density === 'Compact' ? 'compact' : 'comfortable',
  }
}

export interface EmailRow {
  message_id: number
  demand_id: number
  resource_id: number
  result_id: number | null
  rank: number | null
  selected: boolean
  to_address: string
  cc: string[]
  subject: string
  body_html: string
  status: EmailDraft['status'] | 'Sending' | 'Discarded'
  created_at: string
  sent_at: string | null
  last_error: string | null
}
/** Requisition/candidate context comes from matching (#10); until then those fields stay empty. */
export const toEmailDraft = (e: EmailRow): EmailDraft => ({
  emailId: e.message_id,
  resultId: e.result_id ?? 0,
  demandId: e.demand_id,
  codeRequisition: '',
  accountName: null,
  demandRequestName: null,
  candidateName: null,
  rank: e.rank ?? 0,
  score: 0,
  band: 'Insufficient data',
  toMasked: maskEmail(e.to_address),
  cc: e.cc,
  subject: e.subject,
  body: e.body_html,
  status: e.status === 'Sending' ? 'Approved' : e.status === 'Discarded' ? 'Failed' : e.status,
  preselected: e.selected,
  demandUploadedAt: e.created_at,
  sentAt: e.sent_at ?? undefined,
  failureReason: e.last_error ?? undefined,
})

export interface AuditRow {
  audit_id: number
  ts: string
  user_id: number | null
  action: string
  entity_type: string | null
  entity_id: string | null
  details_json: string | null
}
export const toAuditEvent = (a: AuditRow, users: Map<number, OccUser>): AuditEvent => {
  const u = a.user_id == null ? undefined : users.get(a.user_id)
  return {
    auditId: a.audit_id,
    at: a.ts,
    user: u?.fullName ?? (a.user_id == null ? 'System' : `User ${a.user_id}`),
    role: u?.role ?? 'System',
    action: a.action,
    entity: a.entity_type ?? '',
    summary: [a.action, a.entity_type, a.entity_id].filter(Boolean).join(' · '),
    detailsJson: a.details_json ?? undefined,
  }
}

/** `/occ/settings` stores dotted keys; unset keys fall back to the TDD §9.2 defaults. */
export const ADMIN_SETTING_KEYS: Record<keyof AdminSettings, string> = {
  analystMinPasswordLength: 'auth.analyst_min_password_length',
  adminMinPasswordLength: 'auth.admin_min_password_length',
  lockoutAttempts: 'auth.lockout_attempts',
  lockoutMinutes: 'auth.lockout_minutes',
  idleTimeoutMinutes: 'auth.idle_timeout_minutes',
  maskKeep: 'pii.mask_keep',
  maskCount: 'pii.mask_count',
  analystsMayUnmask: 'pii.analysts_may_unmask',
  topN: 'matching.top_n',
  emailsPreselected: 'email.preselected_count',
  staleDataDays: 'data.stale_warning_days',
  emailTurnaroundHours: 'email.turnaround_hours',
  allowExternalProviders: 'llm.allow_external_providers',
}
export function toAdminSettings(row: Record<string, unknown>, defaults: AdminSettings): AdminSettings {
  const out = { ...defaults } as Record<keyof AdminSettings, unknown>
  for (const [k, key] of Object.entries(ADMIN_SETTING_KEYS) as [keyof AdminSettings, string][])
    if (key in row) out[k] = row[key]
  return out as AdminSettings
}
export const fromAdminSettings = (s: AdminSettings): Record<string, unknown> =>
  Object.fromEntries((Object.entries(ADMIN_SETTING_KEYS) as [keyof AdminSettings, string][]).map(([k, key]) => [key, s[k]]))

export interface SynonymRow { synonym_id: number; canonical: string; term: string; relation: string; status: string; created_by: number | null }
const REL: Record<string, Synonym['relation']> = { equivalent: 'Exact synonym', abbreviation: 'Abbreviation', misspelling: 'Spelling variant', related: 'Related' }
const ST: Record<string, Synonym['status']> = { approved: 'Active', suggested: 'Suggested', rejected: 'Rejected' }
export const toSynonym = (r: SynonymRow): Synonym => ({
  synonymId: r.synonym_id,
  canonical: r.canonical,
  variants: [r.term],
  relation: REL[r.relation] ?? 'Related',
  usageCount: 0,
  source: r.status === 'suggested' ? 'system-suggested' : r.created_by == null ? 'seed' : 'admin',
  status: ST[r.status] ?? 'Active',
})
export const fromSynonymRelation = (r: Synonym['relation']) =>
  (Object.entries(REL).find(([, v]) => v === r)?.[0] ?? 'related') as keyof typeof REL

export interface RecipientRow { recipient_id: number; email: string; display_name: string | null; is_active: Flag }
export const toMember = (r: RecipientRow): ResourceTeamMember => ({ email: r.email, displayName: r.display_name ?? '', active: b(r.is_active), notes: '' })
/** `ResourcePage.items` from GET /resources and /resources/excluded (ingestion). */
export interface ResourceItem {
  resource_id: number
  short_name: string
  email_address: string | null
  grade: string | null
  location: string | null
  bench_status: string | null
  bench_status_code: string | null
  skills_raw: string | null
  available_capacity: string | null
  bench_start_date: string | null
  bench_end_date: string | null
  group_name: string | null
  lifecycle: string
  eligibility: string
  exclusion_reasons: string[]
  pending_review: boolean
  unmapped_status: boolean
  occ_pending_review: boolean
}
const ELIGIBILITY: Resource['eligibility'][] = ['eligible', 'eligible_caveat', 'excluded', 'insufficient']
/** The list rows carry no candidate, CV or identity data; those fields take neutral defaults. */
export const toResource = (r: ResourceItem): Resource => ({
  resourceId: r.resource_id,
  candidateId: 0,
  shortName: r.short_name,
  emailAddressMasked: r.email_address ? maskEmail(r.email_address) : null,
  name: null,
  grade: r.grade,
  location: r.location,
  benchStatus: r.bench_status,
  benchStatusPriority: null,
  unmappedStatus: r.unmapped_status,
  skills: (r.skills_raw ?? '').split(/[,;\n]/).map((x) => x.trim()).filter(Boolean),
  availableCapacity: r.available_capacity === 'Y' || r.available_capacity === 'N' ? r.available_capacity : null,
  benchStartDate: r.bench_start_date,
  benchEndDate: r.bench_end_date,
  groupName: r.group_name,
  groupOwner: null,
  eligibility: ELIGIBILITY.find((e) => e === r.eligibility) ?? 'insufficient',
  exclusionReasons: r.exclusion_reasons,
  cvStatus: 'Not synced',
  cvCount: 0,
  candidateType: 'Unknown',
  identityStatus: r.pending_review ? 'incomplete' : 'complete',
})

export const toDomain = (r: { domain: string }): InternalDomain => ({ domain: r.domain })
export const toPreview = (r: { internal: number; external: number; unknown: number }): ClassificationPreview => ({
  internal: r.internal,
  external: r.external,
  unknown: r.unknown,
})
