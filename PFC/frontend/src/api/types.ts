/**
 * DTOs for the `/api/v1` surface (TDD.md §12). Field names follow the SQLite data model
 * (TDD.md §4) in camelCase so the generated OpenAPI client can replace these by name.
 */

export type Role = 'Admin' | 'Analyst'
export type Persona = 'A' | 'B' | 'C'
export type Band = 'Strong' | 'Potential' | 'Avg' | 'Weak' | 'Insufficient data'
export type LocationAssessment = 'Aligned' | 'Remote/unspecified' | 'Potential travel' | 'Location check'
export type GradeRelation = 'exact' | 'adjacent_above' | 'adjacent_below'
export type CandidateType = 'Internal' | 'External' | 'Unknown'
export type IdentityStatus = 'complete' | 'incomplete' | 'ambiguous' | 'review_later'
export type EmailStatus = 'Draft' | 'Approved' | 'Sent' | 'Failed'
export type JdStatus = 'Not analysed' | 'Analysed — awaiting confirmation' | 'Confirmed — scoring' | 'Scored' | 'JD changed'
export type Eligibility = 'eligible' | 'eligible_caveat' | 'excluded' | 'insufficient'
export type UserStatus = 'Active' | 'Disabled' | 'Locked'

/** RFC 9457 problem details returned by the backend on error. */
export interface ProblemDetails {
  type?: string
  title: string
  status: number
  detail?: string
  code?: string
  /** Seconds until a locked account can retry (REQ-AUTH-04). */
  retryAfterSeconds?: number
}

export interface SetupStatus {
  adminExists: boolean
}

export interface SetupAdminRequest {
  setupCode: string
  fullName: string
  email: string
  password: string
}

export interface LoginRequest {
  role: Role
  email: string
  password: string
}

export interface CurrentUser {
  userId: number
  fullName: string
  email: string
  role: Role
  persona?: Persona
  mustChangePassword: boolean
  csrfToken: string
}

export interface ChangePasswordRequest {
  currentPassword: string
  newPassword: string
}

export interface Demand {
  demandId: number
  codeRequisition: string
  idFormatOk: boolean
  positionStartDate: string | null
  accountName: string | null
  codeLocalGrade: string | null
  demandRequestName: string | null
  region: string | null
  bu: string | null
  owner: string | null
  jdText: string
  jdStatus: JdStatus
  matches: number
  bestBand: Band | null
  uploadedAt: string
  mandatorySkills: string[]
  preferredSkills: string[]
}

export interface JdRequirements {
  demandId: number
  mandatorySkills: string[]
  preferredSkills: string[]
  experienceYears: number | null
  qualifications: string
  domain: string
  location: string
  mandatoryOnsite: boolean
  synonymsApplied: string[]
  warnings: string[]
}

export interface Resource {
  resourceId: number
  candidateId: number
  shortName: string
  /** Exact CT value — never constructed or corrected (REQ-IN-14). Masked unless unmasked via /pii/unmask. */
  emailAddressMasked: string | null
  name: string | null
  grade: string | null
  location: string | null
  benchStatus: string | null
  benchStatusPriority: number | null
  unmappedStatus: boolean
  skills: string[]
  /** Display only — never used for availability (REQ-ELIG-06). */
  availableCapacity: 'Y' | 'N' | null
  benchStartDate: string | null
  benchEndDate: string | null
  groupName: string | null
  groupOwner: string | null
  eligibility: Eligibility
  exclusionReasons: string[]
  cvStatus: 'Synced' | 'Not found' | 'Partial' | 'Error' | 'Not synced'
  cvCount: number
  candidateType: CandidateType
  identityStatus: IdentityStatus
}

export interface ScoreComponent {
  component: 'skills' | 'grade' | 'location' | 'bench_start' | 'bench_end' | 'status'
  points: number
  maxPoints: number
}

export interface MatchOverride {
  action: 'pin' | 'exclude' | 'adjust_band'
  newBand?: Band
  reason: string
  createdBy: string
  createdAt: string
}

/** One requisition–person row (REQ-MAT-03, REQ-OUT-01). */
export interface MatchResult {
  resultId: number
  runId: number
  demandId: number
  resourceId: number
  candidateId: number
  rank: number
  codeRequisition: string
  positionStartDate: string | null
  accountName: string | null
  bu: string | null
  demandRequestName: string | null
  demandGrade: string | null
  region: string | null
  personName: string | null
  personEmailMasked: string | null
  personGrade: string | null
  personLocation: string | null
  benchStatus: string | null
  score: number
  band: Band
  gradeRelation: GradeRelation
  locationAssessment: LocationAssessment
  matchingEvidence: string[]
  gapsChecks: string[]
  eligibilityEvidence: string[]
  mandatoryMissing: number
  candidateType: CandidateType
  incompleteIdentity: boolean
  emailPreselected: boolean
  breakdown: ScoreComponent[]
  override?: MatchOverride
}

export interface MatchRun {
  runId: number
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  finishedAt: string | null
  llmModel: string
  templateVersion: number
  demandsAssessed: number
  pairsScored: number
  rolesWithoutCandidate: number
}

export interface ValidationCheck {
  id: string
  label: string
  passed: boolean
  detail?: string
}

export interface ResultFilters {
  codeRequisition?: string
  accountName?: string
  bu?: string
  demandGrade?: string
  personGrade?: string
  location?: string
  region?: string
  band?: Band | ''
  status?: string
  candidateType?: CandidateType | ''
  incompleteIdentity?: 'yes' | 'no' | ''
  mandatorySkills?: 'met' | 'gap' | ''
  scoreMin?: number
  scoreMax?: number
}

export interface CountItem {
  label: string
  count: number
}

export interface AnalyticsSummary {
  run: MatchRun | null
  requisitionsAssessed: number
  openRequisitions: number
  roleToPersonCombinations: number
  rolesWithNoSuitableCandidate: number
  weakOnlyRequisitions: number
  dataQualityIssues: { total: number; dl: number; ct: number }
  eligibleBench: number
  excludedBench: number
  matchCoveragePct: number
  emailsWithin24hPct: number
}

export interface DemandAnalytics {
  openByBuRegion: { bu: string; region: string; count: number }[]
  topSkills: CountItem[]
  gradeToJdCount: CountItem[]
  topAccounts: CountItem[]
  startPipeline: CountItem[]
  /** DL rows ordered by Position Start Date descending (REQ-OUT-05). */
  rows: Demand[]
}

export interface BenchAnalytics {
  topSkills: CountItem[]
  /** Top-5 demanded skills vs eligible bench supply split by Available Capacity (display only, REQ-OUT-06). */
  demandVsSupply: { skill: string; demandCount: number; supplyY: number; supplyN: number }[]
  gradeHistogram: { grade: string; bench: number; demand: number }[]
  timeline: { name: string; start: string; end: string; days: number }[]
  aging: CountItem[]
  statusDistribution: { status: string; count: number; excluded: boolean }[]
  /** CT rows ordered by Bench Start Date descending (REQ-OUT-06). */
  rows: Resource[]
}

export interface MatchAnalytics {
  coveragePct: number
  bandDistribution: CountItem[]
  emailStatusCounts: CountItem[]
  validation: ValidationCheck[]
}

export interface DashboardFilters {
  codeRequisition?: string
  accountName?: string
  bu?: string
  grade?: string
  location?: string
  region?: string
  band?: Band | ''
  status?: string
  candidateType?: CandidateType | ''
  incompleteIdentity?: 'yes' | 'no' | ''
  mandatorySkills?: 'met' | 'gap' | ''
  scoreMin?: number
  scoreMax?: number
}

export interface CvVersion {
  cvVersionId: number
  fileName: string
  folderPath: string
  modifiedAt: string
  extractedAt: string
  isBest: boolean
}

export interface CandidateDetail {
  candidateId: number
  internalRef: string
  resource: Resource
  phoneMasked: string | null
  identityStatus: IdentityStatus
  /** CT emails that may be selected when resolving identity — free entry is disabled (GQ-06). */
  ctEmailOptions: { resourceId: number; label: string; emailMasked: string }[]
  cvVersions: CvVersion[]
  matches: MatchResult[]
  activity: { at: string; summary: string }[]
}

export interface IdentityResolution {
  action: 'generate_internal_id' | 'select_ct_record' | 'review_later'
  resourceId?: number
}

export interface EmailDraft {
  emailId: number
  resultId: number
  demandId: number
  codeRequisition: string
  accountName: string | null
  demandRequestName: string | null
  candidateName: string | null
  rank: number
  score: number
  band: Band
  toMasked: string | null
  cc: string[]
  subject: string
  body: string
  status: EmailStatus
  preselected: boolean
  demandUploadedAt: string
  sentAt?: string
  failureReason?: string
  previouslySentAt?: string
}

export interface EmailTransportStatus {
  transport: 'Outlook COM' | 'SMTP'
  outlookDetected: boolean
}

export interface PersonalSettings {
  resumeFolderPath: string
  autoSyncDl: { enabled: boolean; watchPath: string; pattern: string }
  autoSyncCt: { enabled: boolean; watchPath: string; pattern: string }
  activeModel: string
  spoc1: string
  spoc2: string
  emailTemplate: string
  density: 'Comfortable' | 'Compact'
}

export interface LlmModel {
  id: string
  name: string
  purpose: 'generation' | 'embedding'
  ramGb: number
  diskGb: number
  licence: string
  approved: boolean
  isDefault: boolean
}

export interface OccUser {
  userId: number
  fullName: string
  email: string
  role: Role
  persona?: Persona
  status: UserStatus
  lastSignIn: string | null
  passwordSetAt: string | null
}

export interface NewUserRequest {
  fullName: string
  email: string
  role: Role
  persona?: Persona
}

export interface OneTimePassword {
  userId: number
  oneTimePassword: string
}

export interface BenchStatus {
  code: string
  label: string
  eligibility: 'Eligible' | 'Eligible with caveat' | 'Excluded'
  priority: number | null
  tieredPoints: number
  caveat: string
}

export interface StatusMapEntry {
  sourceText: string
  canonicalCode: string
}

export interface Synonym {
  synonymId: number
  canonical: string
  variants: string[]
  relation: 'Exact synonym' | 'Abbreviation' | 'Spelling variant' | 'Related'
  usageCount: number
  source: 'seed' | 'system-suggested' | 'admin'
  status: 'Active' | 'Suggested' | 'Rejected'
}

export interface ScoringWeights {
  skills: number
  grade: number
  location: number
  benchStart: number
  benchEnd: number
  status: number
}

export interface ScoringTemplate {
  templateId: number
  version: number
  status: 'draft' | 'active' | 'retired'
  weights: ScoringWeights
  adjacentGradePoints: number
  statusScoreMode: 'binary' | 'tiered'
  bands: { strong: number; potential: number; avg: number }
  changeNote: string
  createdAt: string
  createdBy: string
}

export interface ResourceTeamMember {
  email: string
  displayName: string
  active: boolean
  notes: string
}

export interface InternalDomain {
  domain: string
}

export interface ClassificationPreview {
  internal: number
  external: number
  unknown: number
}

export interface SettingsPackageInfo {
  version: number
  fileName: string
  createdAt: string
  signed: boolean
  contents: CountItem[]
}

export interface PackageChange {
  area: string
  change: 'added' | 'changed' | 'removed'
  item: string
  before?: string
  after?: string
}

export interface PackagePreview {
  previewId: string
  fileName: string
  packageVersion: number
  installedVersion: number
  signatureValid: boolean
  schemaCompatible: boolean
  appCompatible: boolean
  newer: boolean
  changes: PackageChange[]
  importedAccounts: number
  errors: string[]
}

export interface PackageImportRecord {
  version: number
  importedAt: string
  importedBy: string
  fileName: string
  canRollback: boolean
}

export interface UploadPreview {
  ingestionRunId: number
  kind: 'dl' | 'ct'
  fileName: string
  sheet: string
  rowsRead: number
  new: number
  updated: number
  unchanged: number
  rejected: number
  warnings: string[]
  alreadyImported: boolean
}
