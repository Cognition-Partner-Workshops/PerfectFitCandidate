import type {
  ExcludedEntry,
  AdminSettings,
  ApprovalDecision,
  ApprovalEntry,
  AuditEvent,
  AuditQuery,
  Grade,
  AnalyticsSummary,
  BenchAnalytics,
  BenchStatus,
  CandidateDetail,
  CandidateClassification,
  CandidateType,
  CvExtraction,
  RemovalCounts,
  ChangePasswordRequest,
  ClassificationPreview,
  CurrentUser,
  DashboardFilters,
  Demand,
  DemandAnalytics,
  EmailDraft,
  EmailTransportStatus,
  ExportedPackage,
  IdentityResolution,
  InternalDomain,
  JdRequirements,
  LlmModel,
  LoginRequest,
  MatchAnalytics,
  MatchOverride,
  MatchResult,
  MatchRun,
  NewUserRequest,
  OccUser,
  OneTimePassword,
  PackageImportRecord,
  PackagePreview,
  PackageSource,
  PendingPackage,
  PersonalSettings,
  ProblemDetails,
  Resource,
  ResultFilters,
  ResourceTeamMember,
  Role,
  ScoringTemplate,
  EmailTemplateDraft,
  EmailTemplateVersion,
  SettingsPackageInfo,
  SetupAdminRequest,
  SetupStatus,
  StatusMapEntry,
  Synonym,
  UploadPreview,
  UserStatus,
  SystemHealth,
} from './types'

/** Error carrying an RFC 9457 problem document. */
export class ApiError extends Error {
  readonly problem: ProblemDetails

  constructor(problem: ProblemDetails) {
    super(problem.detail ?? problem.title)
    this.name = 'ApiError'
    this.problem = problem
  }

  get status(): number {
    return this.problem.status
  }

  get code(): string | undefined {
    return this.problem.code
  }
}

export type ExportFormat = 'csv' | 'xlsx'

export interface ExportedFile {
  blob: Blob
  fileName: string
}

export interface EmailList {
  items: EmailDraft[]
  transport: EmailTransportStatus
}

export interface StatusMapData {
  entries: StatusMapEntry[]
  unmapped: string[]
}

/**
 * Typed client over the `/api/v1` surface (TDD.md §12). The comment on each method is
 * the HTTP route it maps to. Implemented by `HttpApiClient` (real backend) and
 * `MockApiClient` (in-browser fixtures), selected by `VITE_PFC_API`.
 */
export interface ApiClient {
  /** GET /setup/status */
  getSetupStatus(): Promise<SetupStatus>
  /** POST /setup/admin */
  setupAdmin(req: SetupAdminRequest): Promise<void>
  /** POST /auth/login */
  login(req: LoginRequest): Promise<CurrentUser>
  /** POST /auth/logout */
  logout(): Promise<void>
  /** GET /auth/me */
  me(): Promise<CurrentUser | null>
  /** POST /auth/password */
  changePassword(req: ChangePasswordRequest): Promise<CurrentUser>

  /** GET /demands */
  listDemands(): Promise<Demand[]>
  /** POST /demands/analyze */
  analyzeDemands(demandIds: number[]): Promise<void>
  /** POST /demands/{id}/analyze — extracts a new requirements draft */
  analyzeDemand(demandId: number): Promise<JdRequirements>
  /** GET /demands/{id}/requirements */
  getRequirements(demandId: number): Promise<JdRequirements>
  /** PUT /demands/{id}/requirements */
  saveRequirements(req: JdRequirements): Promise<JdRequirements>
  /** POST /demands/{id}/requirements/confirm */
  confirmRequirements(demandId: number): Promise<void>
  /** POST /uploads/{kind} */
  uploadWorkbook(kind: 'dl' | 'ct', file: File): Promise<UploadPreview>
  /** POST /ingestion-runs/{id}/commit */
  commitIngestion(ingestionRunId: number): Promise<void>
  /** POST /ingestion-runs/{id}/discard */
  discardIngestion(ingestionRunId: number): Promise<void>

  /** GET /resources */
  listResources(): Promise<Resource[]>
  /** GET /resources?lifecycle=… for every lifecycle, so people who have since left the bench still resolve. */
  listAllResources(): Promise<Resource[]>
  /** GET /resources/excluded */
  listExcludedResources(): Promise<Resource[]>
  /** POST /resources/cv-sync (ids ≥ 1) */
  syncCvs(resourceIds: number[]): Promise<void>

  /** GET /candidates/{id} */
  getCandidate(candidateId: number): Promise<CandidateDetail>
  setClassification(candidateId: number, body: { classification: CandidateType | null; reason: string }): Promise<CandidateClassification>
  listCvExtractions(candidateId: number): Promise<CvExtraction[]>
  clearCandidateExtractions(candidateId: number): Promise<RemovalCounts>
  clearAllExtractions(): Promise<RemovalCounts>
  /** PUT /candidates/{id}/identity */
  resolveIdentity(candidateId: number, resolution: IdentityResolution): Promise<CandidateDetail>

  /** GET /match-runs */
  listMatchRuns(): Promise<MatchRun[]>
  /** GET /match-runs/{id} — polled while a run is queued or running */
  getMatchRun(runId: number): Promise<MatchRun>
  /** GET /match-runs/{id}/results */
  getResults(runId: number): Promise<MatchResult[]>
  /** GET /match-runs/{id}/excluded — people the run did not score, with reason codes (REQ-ELIG-08). */
  getRunExcluded(runId: number): Promise<ExcludedEntry[]>
  /** POST /match-results/{id}/overrides */
  addOverride(resultId: number, override: Omit<MatchOverride, 'createdBy' | 'createdAt'>): Promise<MatchResult>
  /** POST /pii/unmask — audited (REQ-OUT-03, REQ-AUTH-05) */
  unmask(entity: 'resource' | 'candidate' | 'occ_user', id: number, field: 'email' | 'phone'): Promise<string>

  /** GET /match-runs/{id}/export?format=csv|xlsx — emails masked unless includeEmails (REQ-OUT-09). */
  exportResults(runId: number, filters: ResultFilters, format: ExportFormat, includeEmails: boolean): Promise<ExportedFile>
  /** GET /analytics/summary */
  analyticsSummary(filters: DashboardFilters): Promise<AnalyticsSummary>
  /** GET /analytics/demand */
  analyticsDemand(filters: DashboardFilters): Promise<DemandAnalytics>
  /** GET /analytics/bench */
  analyticsBench(filters: DashboardFilters): Promise<BenchAnalytics>
  /** GET /analytics/match */
  analyticsMatch(filters: DashboardFilters): Promise<MatchAnalytics>

  /** POST /emails/drafts */
  createDrafts(resultIds: number[], runId?: number): Promise<EmailDraft[]>
  /** GET /emails */
  listEmails(): Promise<EmailList>
  /** PUT /emails/{id} */
  updateEmail(emailId: number, patch: Partial<Pick<EmailDraft, 'subject' | 'body' | 'cc' | 'status'>>): Promise<EmailDraft>
  /** POST /emails/{id}/approve */
  approveEmail(emailId: number, confirmResend?: boolean): Promise<EmailDraft>
  /** POST /emails/{id}/send */
  sendEmail(emailId: number): Promise<EmailDraft>

  /** GET /settings/me */
  getMySettings(): Promise<PersonalSettings>
  /** PUT /settings/me */
  saveMySettings(settings: PersonalSettings): Promise<PersonalSettings>
  /** GET /llm/models */
  listModels(): Promise<LlmModel[]>
  /** POST /settings/settings-package/preview */
  previewPackage(file: File): Promise<PackagePreview>
  /** POST /settings/settings-package/import */
  importPackage(source: PackageSource): Promise<PackageImportRecord[]>
  /** GET /settings/settings-package/pending */
  pendingPackages(): Promise<PendingPackage[]>
  /** POST /settings/settings-package/rollback */
  rollbackPackage(): Promise<PackageImportRecord[]>
  /** GET /settings/settings-package/history */
  packageHistory(): Promise<PackageImportRecord[]>

  /** GET /occ/users */
  listUsers(): Promise<OccUser[]>
  /** POST /occ/users */
  createUser(req: NewUserRequest): Promise<OneTimePassword>
  /** POST /occ/users/{id}/reset-password */
  resetPassword(userId: number): Promise<OneTimePassword>
  /** POST /occ/users/{id}/unlock */
  unlockUser(userId: number): Promise<OccUser>
  /** PUT /occ/users/{id} */
  updateUser(userId: number, patch: { status?: UserStatus; role?: Role }): Promise<OccUser>

  /** GET /occ/bench-statuses */
  listBenchStatuses(): Promise<BenchStatus[]>
  /** PUT /occ/bench-statuses */
  saveBenchStatuses(statuses: BenchStatus[]): Promise<BenchStatus[]>
  /** GET /occ/status-map */
  getStatusMap(): Promise<StatusMapData>
  /** PUT /occ/status-map */
  saveStatusMap(entries: StatusMapEntry[]): Promise<StatusMapData>

  /** GET /occ/synonyms */
  listSynonyms(): Promise<Synonym[]>
  /** POST /occ/synonyms */
  createSynonym(s: Omit<Synonym, 'synonymId' | 'usageCount' | 'source' | 'status'>): Promise<Synonym>
  /** PUT /occ/synonyms/{id} */
  updateSynonym(synonymId: number, patch: Partial<Synonym>): Promise<Synonym>

  /** GET /occ/scoring-templates */
  listTemplates(): Promise<ScoringTemplate[]>
  /** POST /occ/scoring-templates then POST /occ/scoring-templates/{id}/activate */
  publishTemplate(draft: Omit<ScoringTemplate, 'templateId' | 'version' | 'status' | 'createdAt' | 'createdBy'>): Promise<ScoringTemplate[]>

  /** GET /occ/email-templates (latest version per name; `all` lists every version) */
  listEmailTemplates(all: boolean): Promise<EmailTemplateVersion[]>
  /** GET /occ/email-templates/{id} */
  getEmailTemplate(emailTemplateId: number): Promise<EmailTemplateVersion>
  /** POST /occ/email-templates — appends the next version of `name` */
  createEmailTemplate(draft: EmailTemplateDraft): Promise<EmailTemplateVersion>
  /** POST /occ/email-templates/{id}/set-default */
  setDefaultEmailTemplate(emailTemplateId: number): Promise<EmailTemplateVersion>

  /** GET /occ/resource-team */
  listResourceTeam(): Promise<ResourceTeamMember[]>
  /** PUT /occ/resource-team */
  saveResourceTeam(members: ResourceTeamMember[]): Promise<ResourceTeamMember[]>

  /** GET /occ/internal-domains */
  listInternalDomains(): Promise<InternalDomain[]>
  /** PUT /occ/internal-domains */
  saveInternalDomains(domains: InternalDomain[]): Promise<InternalDomain[]>
  /** POST /occ/internal-domains/preview */
  previewClassification(domains: InternalDomain[]): Promise<ClassificationPreview>

  /** GET /occ/settings-package */
  lastExportedPackage(): Promise<SettingsPackageInfo | null>
  /** POST /occ/settings-package/export */
  exportPackage(passphrase: string): Promise<ExportedPackage>

  /** GET /occ/approvals (DL/CT entries pending OCC review) */
  listApprovals(): Promise<ApprovalEntry[]>
  /** PUT /occ/demands/{id} or /occ/resources/{id} — approve, modify (reason required) or revert */
  decideApproval(entry: ApprovalEntry, decision: ApprovalDecision): Promise<ApprovalEntry>

  /** GET /occ/grades */
  listGrades(): Promise<Grade[]>
  /** PUT /occ/grades */
  saveGrades(grades: Grade[]): Promise<Grade[]>

  /** GET /occ/llm-models */
  listOccModels(): Promise<LlmModel[]>
  /** PUT /occ/llm-models/{id} */
  updateOccModel(id: string, patch: Partial<Pick<LlmModel, 'approved' | 'isDefault'>>): Promise<LlmModel[]>

  /** GET /occ/audit-log */
  auditLog(query: AuditQuery): Promise<AuditEvent[]>

  /** GET /health — liveness plus the data volume's BitLocker status (REQ-AUTH-06) */
  getHealth(): Promise<SystemHealth>

  /** GET /occ/settings */
  getAdminSettings(): Promise<AdminSettings>
  /** PUT /occ/settings */
  saveAdminSettings(settings: AdminSettings): Promise<AdminSettings>
}
