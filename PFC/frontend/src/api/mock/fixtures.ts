/**
 * Illustrative mock data for the typed mock client. All people, accounts and requisitions are
 * fictitious; email addresses use the reserved `example.com` / `example.org` domains (RFC 2606)
 * and stand in for values copied verbatim from a Candidate Tracker.
 */
import type { BenchStatus, Demand, LlmModel, ResourceTeamMember, StatusMapEntry, Synonym, AdminSettings, ApprovalEntry, Grade } from '../types'

export interface RawResource {
  resourceId: number
  shortName: string
  email: string | null
  phone: string | null
  name: string | null
  grade: string | null
  location: string | null
  benchStatus: string | null
  skills: string[]
  availableCapacity: 'Y' | 'N' | null
  benchStartDate: string | null
  benchEndDate: string | null
  groupName: string | null
  groupOwner: string | null
  cvCount: number
  cvStatus: 'Synced' | 'Not found' | 'Partial' | 'Error' | 'Not synced'
  identityStatus: 'complete' | 'incomplete' | 'ambiguous' | 'review_later'
}

export type DemandSeed = Omit<Demand, 'matches' | 'bestBand'>

const up = '2026-11-04T09:12:00Z'

export const DEMANDS: DemandSeed[] = [
  { demandId: 1, codeRequisition: 'T1A123', idFormatOk: true, positionStartDate: '2026-12-15', accountName: 'Acme Bank', codeLocalGrade: 'D1', demandRequestName: 'TC14_Data Business Analyst_UK_London_D1_BUK', region: 'London', bu: 'BCM', owner: 'J. Doe', jdText: '• 5+ years business analysis in banking\n• SQL and data mapping\n• Stakeholder management', jdStatus: 'Scored', uploadedAt: up, mandatorySkills: ['Business Analysis', 'SQL'], preferredSkills: ['Data Mapping', 'Agile'] },
  { demandId: 2, codeRequisition: 'T2B456', idFormatOk: true, positionStartDate: '2027-01-02', accountName: 'Contoso Insurance', codeLocalGrade: 'C2', demandRequestName: 'Snowflake Data Engineer', region: 'Glasgow', bu: 'INS', owner: 'A. Rao', jdText: '• Snowflake, dbt, Airflow\n• Python\n• Financial services desirable', jdStatus: 'Scored', uploadedAt: '2026-11-05T08:00:00Z', mandatorySkills: ['Snowflake', 'SQL', 'dbt'], preferredSkills: ['Python', 'Airflow'] },
  { demandId: 3, codeRequisition: 'T2C900', idFormatOk: true, positionStartDate: '2026-11-20', accountName: 'Fabrikam', codeLocalGrade: 'C1', demandRequestName: 'Scrum Master', region: null, bu: 'BCM', owner: 'M. Li', jdText: '• Certified Scrum Master\n• Jira, coaching', jdStatus: 'Scored', uploadedAt: '2026-11-03T07:30:00Z', mandatorySkills: ['Scrum', 'Jira'], preferredSkills: ['Coaching'] },
  { demandId: 4, codeRequisition: 'T1D210', idFormatOk: true, positionStartDate: '2027-02-01', accountName: 'Acme Bank', codeLocalGrade: 'C2', demandRequestName: 'Databricks Engineer', region: 'Edinburgh', bu: 'BCM', owner: 'J. Doe', jdText: '• Databricks, PySpark\n• Azure', jdStatus: 'Scored', uploadedAt: up, mandatorySkills: ['Databricks', 'Python'], preferredSkills: ['Azure', 'SQL'] },
  { demandId: 5, codeRequisition: 'T1E333', idFormatOk: true, positionStartDate: '2026-12-01', accountName: 'Northwind Retail', codeLocalGrade: 'D2', demandRequestName: 'Delivery Lead', region: 'Manchester', bu: 'RCG', owner: 'S. Patel', jdText: '• Programme delivery\n• Project Management', jdStatus: 'Scored', uploadedAt: up, mandatorySkills: ['Project Management'], preferredSkills: ['Agile', 'Stakeholder Management'] },
  { demandId: 6, codeRequisition: 'T2F777', idFormatOk: true, positionStartDate: '2027-03-10', accountName: 'Contoso Insurance', codeLocalGrade: 'B2', demandRequestName: 'Data Analyst (Power BI)', region: 'London', bu: 'INS', owner: 'A. Rao', jdText: '• Power BI, SQL, DAX', jdStatus: 'Scored', uploadedAt: up, mandatorySkills: ['Power BI', 'SQL'], preferredSkills: ['Data Analysis'] },
  { demandId: 7, codeRequisition: 'T1G101', idFormatOk: true, positionStartDate: '2027-01-15', accountName: 'Tailspin Energy', codeLocalGrade: 'C1', demandRequestName: 'GenAI Engineer', region: 'London', bu: 'ENU', owner: 'K. Mensah', jdText: '• Generative AI, Python, LLMs', jdStatus: 'Analysed — awaiting confirmation', uploadedAt: up, mandatorySkills: ['GenAI', 'Python'], preferredSkills: ['Azure'] },
  { demandId: 8, codeRequisition: 'ABCD123', idFormatOk: false, positionStartDate: '2026-11-25', accountName: 'Fabrikam', codeLocalGrade: 'C1', demandRequestName: 'Test Analyst', region: 'Remote', bu: 'BCM', owner: 'M. Li', jdText: '• Test automation, Selenium', jdStatus: 'Not analysed', uploadedAt: up, mandatorySkills: [], preferredSkills: [] },
  { demandId: 9, codeRequisition: 'T2H404', idFormatOk: true, positionStartDate: '2027-02-20', accountName: 'Northwind Retail', codeLocalGrade: 'D1', demandRequestName: 'Data Migration Lead', region: 'Leeds', bu: 'RCG', owner: 'S. Patel', jdText: '• Data migration, reconciliation, profiling', jdStatus: 'Scored', uploadedAt: up, mandatorySkills: ['Data Migration', 'SQL'], preferredSkills: ['Data Quality', 'Project Management'] },
  { demandId: 10, codeRequisition: 'T1J550', idFormatOk: true, positionStartDate: '2027-04-01', accountName: 'Acme Bank', codeLocalGrade: 'E1', demandRequestName: 'Engagement Director', region: 'London', bu: 'BCM', owner: 'J. Doe', jdText: '• Account leadership', jdStatus: 'Not analysed', uploadedAt: up, mandatorySkills: [], preferredSkills: [] },
]

export const RESOURCES: RawResource[] = [
  { resourceId: 1, shortName: 'ALEXANDER', email: 'AlexAlexander@example.com', phone: '+44 7700 900189', name: 'Alex Alexander', grade: 'D1', location: 'London', benchStatus: 'Available', skills: ['SQL', 'Business Analysis', 'Data Mapping', 'Agile'], availableCapacity: 'Y', benchStartDate: '2026-10-12', benchEndDate: null, groupName: 'RG2', groupOwner: 'J. Doe', cvCount: 2, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 2, shortName: 'BSMITH', email: 'BeaSmith@example.com', phone: null, name: 'Bea Smith', grade: 'C2', location: 'Glasgow', benchStatus: 'CV with client - Demand Proposed', skills: ['Project Management', 'Agile', 'Business Analysis', 'SQL'], availableCapacity: 'N', benchStartDate: '2026-09-01', benchEndDate: null, groupName: 'RG1', groupOwner: 'A. Rao', cvCount: 0, cvStatus: 'Not found', identityStatus: 'complete' },
  { resourceId: 3, shortName: 'CPATEL', email: 'ChandraPatel@example.com', phone: '+44 7700 900321', name: 'Chandra Patel', grade: 'C2', location: 'Edinburgh/Glasgow', benchStatus: 'Available', skills: ['Snowflake', 'SQL', 'dbt', 'Python', 'Airflow'], availableCapacity: 'Y', benchStartDate: '2026-10-20', benchEndDate: null, groupName: 'RG3', groupOwner: 'K. Mensah', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 4, shortName: 'DOKAFOR', email: 'DanielOkafor@example.com', phone: null, name: 'Daniel Okafor', grade: 'C1', location: 'Glasgow', benchStatus: 'Overall upskilling (New bench)', skills: ['Snowflake', 'SQL', 'Python', 'Databricks'], availableCapacity: 'N', benchStartDate: '2026-10-28', benchEndDate: null, groupName: 'RG2', groupOwner: 'J. Doe', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 5, shortName: 'ENOVAK', email: 'EvaNovak@example.org', phone: null, name: 'Eva Novak', grade: 'D2', location: 'Manchester', benchStatus: 'Available', skills: ['Project Management', 'Agile', 'Stakeholder Management', 'Scrum'], availableCapacity: 'Y', benchStartDate: '2026-08-15', benchEndDate: null, groupName: 'RG4', groupOwner: 'S. Patel', cvCount: 3, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 6, shortName: 'FGARCIA', email: 'FelipeGarcia@example.com', phone: null, name: 'Felipe Garcia', grade: 'C1', location: 'London', benchStatus: 'Expected on Bench Soon', skills: ['Scrum', 'Jira', 'Coaching', 'Agile'], availableCapacity: 'Y', benchStartDate: '2026-11-18', benchEndDate: null, groupName: 'RG1', groupOwner: 'M. Li', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 7, shortName: 'GHUANG', email: 'GraceHuang@example.com', phone: null, name: 'Grace Huang', grade: 'B2', location: 'London', benchStatus: 'Available', skills: ['Power BI', 'SQL', 'Data Analysis', 'BI'], availableCapacity: 'N', benchStartDate: '2026-10-05', benchEndDate: null, groupName: 'RG2', groupOwner: 'A. Rao', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 8, shortName: 'HKHAN', email: 'HassanKhan@example.com', phone: null, name: 'Hassan Khan', grade: 'C2', location: 'Edinburgh', benchStatus: 'Available', skills: ['Databricks', 'Python', 'Azure', 'SQL'], availableCapacity: 'Y', benchStartDate: '2026-09-22', benchEndDate: null, groupName: 'RG3', groupOwner: 'K. Mensah', cvCount: 2, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 9, shortName: 'IMORRIS', email: 'IsabelMorris@example.com', phone: null, name: 'Isabel Morris', grade: 'D1', location: 'Leeds', benchStatus: 'Available', skills: ['Data Migration', 'SQL', 'Data Quality', 'Reconciliation'], availableCapacity: 'N', benchStartDate: '2026-07-30', benchEndDate: null, groupName: 'RG4', groupOwner: 'S. Patel', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 10, shortName: 'JSMITH', email: null, phone: null, name: 'J Smith', grade: 'C2', location: 'London', benchStatus: 'Available', skills: ['SQL', 'Business Analysis'], availableCapacity: null, benchStartDate: '2026-10-30', benchEndDate: null, groupName: 'RG1', groupOwner: 'J. Doe', cvCount: 1, cvStatus: 'Partial', identityStatus: 'ambiguous' },
  { resourceId: 11, shortName: 'KLEE', email: 'KevinLee@example.com', phone: null, name: 'Kevin Lee', grade: 'C1', location: 'Glasgow', benchStatus: 'Selected', skills: ['Snowflake', 'SQL'], availableCapacity: 'Y', benchStartDate: '2026-09-10', benchEndDate: null, groupName: 'RG2', groupOwner: 'A. Rao', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 12, shortName: 'LMARTIN', email: 'LauraMartin@example.com', phone: null, name: 'Laura Martin', grade: 'D1', location: 'London', benchStatus: 'Available', skills: ['Business Analysis', 'Agile'], availableCapacity: 'N', benchStartDate: '2026-08-01', benchEndDate: '2026-11-01', groupName: 'RG2', groupOwner: 'J. Doe', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 13, shortName: 'MBROWN', email: 'MiaBrown@example.com', phone: null, name: 'Mia Brown', grade: 'B2', location: 'Leeds', benchStatus: 'Available', skills: ['Data Analysis', 'SQL', 'Power BI'], availableCapacity: 'Y', benchStartDate: '2026-10-14', benchEndDate: null, groupName: 'Not on Bench', groupOwner: 'S. Patel', cvCount: 0, cvStatus: 'Not synced', identityStatus: 'complete' },
  { resourceId: 14, shortName: 'NWRIGHT', email: 'NoahWright@example.com', phone: null, name: 'Noah Wright', grade: 'C2', location: 'Manchester', benchStatus: 'CV with Client - Sold - Pending Allocation', skills: ['Project Management', 'Scrum'], availableCapacity: 'N', benchStartDate: '2026-09-05', benchEndDate: null, groupName: 'RG4', groupOwner: 'S. Patel', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 15, shortName: 'OADEYEMI', email: 'OluAdeyemi@example.org', phone: null, name: 'Olu Adeyemi', grade: 'C1', location: 'London', benchStatus: 'Bench - awaiting role', skills: ['GenAI', 'Python', 'Azure'], availableCapacity: 'Y', benchStartDate: '2026-10-25', benchEndDate: null, groupName: 'RG5', groupOwner: 'K. Mensah', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 16, shortName: 'PSINGH', email: 'PriyaSingh@example.com', phone: null, name: 'Priya Singh', grade: null, location: 'Birmingham', benchStatus: 'Available', skills: ['SQL'], availableCapacity: 'N', benchStartDate: '2026-10-01', benchEndDate: null, groupName: 'RG3', groupOwner: 'K. Mensah', cvCount: 0, cvStatus: 'Not found', identityStatus: 'complete' },
  { resourceId: 17, shortName: 'QROSS', email: 'QuinnRoss@example.com', phone: null, name: 'Quinn Ross', grade: 'D2', location: 'London', benchStatus: 'Available', skills: ['Project Management', 'Data Migration', 'Stakeholder Management'], availableCapacity: 'Y', benchStartDate: '2026-06-20', benchEndDate: null, groupName: 'RG4', groupOwner: 'S. Patel', cvCount: 2, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 19, shortName: 'SBAKER', email: 'SamBaker@example.com', phone: null, name: 'Sam Baker', grade: 'D2', location: 'London', benchStatus: 'Available', skills: ['Business Analysis', 'SQL', 'Agile', 'Stakeholder Management'], availableCapacity: 'Y', benchStartDate: '2026-10-02', benchEndDate: null, groupName: 'RG1', groupOwner: 'J. Doe', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 20, shortName: 'TWILSON', email: 'TaraWilson@example.org', phone: null, name: 'Tara Wilson', grade: 'C2', location: 'London', benchStatus: 'CV with client - Proactive Proposed', skills: ['SQL', 'Business Analysis', 'Data Mapping'], availableCapacity: 'N', benchStartDate: '2026-09-15', benchEndDate: null, groupName: 'RG2', groupOwner: 'A. Rao', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
  { resourceId: 18, shortName: 'RCHEN', email: 'RuiChen@example.com', phone: null, name: 'Rui Chen', grade: 'C1', location: 'Edinburgh', benchStatus: 'Unknown', skills: ['Databricks', 'Python'], availableCapacity: 'N', benchStartDate: '2026-10-08', benchEndDate: null, groupName: 'RG3', groupOwner: 'K. Mensah', cvCount: 1, cvStatus: 'Synced', identityStatus: 'complete' },
]

export const BENCH_STATUSES: BenchStatus[] = [
  { code: 'AVAILABLE', label: 'Available', eligibility: 'Eligible', priority: 1, tieredPoints: 5, caveat: '' },
  { code: 'UPSKILLING', label: 'Overall upskilling (New bench)', eligibility: 'Eligible', priority: 2, tieredPoints: 4, caveat: '' },
  { code: 'CV_DEMAND', label: 'CV with client - Demand Proposed', eligibility: 'Eligible with caveat', priority: 3, tieredPoints: 3, caveat: 'Proposed for another demand' },
  { code: 'CV_PROACTIVE', label: 'CV with client - Proactive Proposed', eligibility: 'Eligible with caveat', priority: 3, tieredPoints: 3, caveat: 'Proactively proposed to a client' },
  { code: 'SOON', label: 'Expected on Bench Soon', eligibility: 'Eligible', priority: 4, tieredPoints: 2, caveat: 'Available from Bench Start Date' },
  { code: 'STATUS_X', label: 'Status - x', eligibility: 'Eligible', priority: 5, tieredPoints: 1, caveat: '' },
  { code: 'STATUS_Y', label: 'Status - y', eligibility: 'Eligible', priority: 6, tieredPoints: 1, caveat: '' },
  { code: 'SELECTED', label: 'Selected', eligibility: 'Excluded', priority: null, tieredPoints: 0, caveat: '' },
  { code: 'UNKNOWN', label: 'Unknown', eligibility: 'Excluded', priority: null, tieredPoints: 0, caveat: '' },
  { code: 'SOLD', label: 'CV with Client - Sold - Pending Allocation', eligibility: 'Excluded', priority: null, tieredPoints: 0, caveat: '' },
]

export const STATUS_MAP: StatusMapEntry[] = BENCH_STATUSES.map((s) => ({ sourceText: s.label, canonicalCode: s.code }))

export const SYNONYMS: Synonym[] = [
  { synonymId: 1, canonical: 'Business Analysis', variants: ['BA', 'Business Analyst', 'Bussiness Analysis'], relation: 'Exact synonym', usageCount: 23, source: 'seed', status: 'Active' },
  { synonymId: 2, canonical: 'Project Management', variants: ['PM', 'Project Manager'], relation: 'Abbreviation', usageCount: 17, source: 'seed', status: 'Active' },
  { synonymId: 3, canonical: 'Data Analysis', variants: ['Data Analyst', 'BI'], relation: 'Related', usageCount: 12, source: 'seed', status: 'Active' },
  { synonymId: 4, canonical: 'Databricks', variants: ['DataBricks'], relation: 'Spelling variant', usageCount: 9, source: 'seed', status: 'Active' },
  { synonymId: 5, canonical: 'GenAI', variants: ['Generative AI', 'AI'], relation: 'Related', usageCount: 6, source: 'seed', status: 'Active' },
  { synonymId: 6, canonical: 'Data Migration', variants: ['data mapping', 'reconciliation', 'profiling', 'data quality'], relation: 'Related', usageCount: 8, source: 'seed', status: 'Active' },
  { synonymId: 7, canonical: 'dbt', variants: ['data build tool'], relation: 'Exact synonym', usageCount: 3, source: 'admin', status: 'Active' },
  { synonymId: 8, canonical: 'Snowflake', variants: ['SnowFlake', 'snow flake'], relation: 'Spelling variant', usageCount: 7, source: 'system-suggested', status: 'Suggested' },
]

export const MODELS: LlmModel[] = [
  { id: 'qwen3-4b', name: 'Qwen3-4B-Instruct-2507 (Q4_K_M)', purpose: 'generation', ramGb: 3.6, diskGb: 2.5, licence: 'Apache-2.0', approved: true, isDefault: true },
  { id: 'phi4-mini', name: 'Phi-4-mini-instruct (Q4_K_M)', purpose: 'generation', ramGb: 3.4, diskGb: 2.4, licence: 'MIT', approved: true, isDefault: false },
  { id: 'llama32-3b', name: 'Llama-3.2-3B-Instruct (Q4_K_M)', purpose: 'generation', ramGb: 3.0, diskGb: 2.0, licence: 'Llama 3.2 Community', approved: false, isDefault: false },
  { id: 'nomic-embed', name: 'nomic-embed-text-v1.5 (ONNX)', purpose: 'embedding', ramGb: 0.6, diskGb: 0.27, licence: 'Apache-2.0', approved: true, isDefault: true },
]

/** OCC DL/CT approval queue seed (GUI_OCC_DESIGN.md §16.4). Fictitious data only. */
export const APPROVALS: ApprovalEntry[] = [
  { entryId: 501, source: 'DL', targetId: DEMANDS.find((d) => d.codeRequisition === 'T1A123')?.demandId ?? 1, key: 'T1A123', change: 'Updated', uploadedBy: 'Analyst upload', uploadedAt: '2026-11-05T09:12:00Z', state: 'Pending', conflict: false, fields: [{ field: 'Position Start Date', before: '2026-12-01', after: '2026-12-15' }] },
  { entryId: 502, source: 'DL', targetId: 9001, key: 'T3N777', change: 'New', uploadedBy: 'Analyst upload', uploadedAt: '2026-11-05T09:12:00Z', state: 'Pending', conflict: false, fields: [{ field: 'Demand Request Name', before: null, after: 'Azure Platform Engineer' }, { field: 'Code Local Grade', before: null, after: 'C1' }] },
  { entryId: 503, source: 'CT', targetId: RESOURCES.find((r) => r.shortName === 'ALEXANDER')?.resourceId ?? 1, key: 'ALEXANDER · Al******ander@example.com', change: 'Updated', uploadedBy: 'Analyst upload', uploadedAt: '2026-11-05T10:40:00Z', state: 'Pending', conflict: true, fields: [{ field: 'Bench Status', before: 'Available', after: 'CV with client - Proactive Proposed', adminValue: 'Available' }] },
  { entryId: 504, source: 'CT', targetId: RESOURCES.find((r) => r.shortName === 'LMARTIN')?.resourceId ?? 2, key: 'LMARTIN · Lu******tin@example.com', change: 'Removed', uploadedBy: 'Analyst upload', uploadedAt: '2026-11-04T16:05:00Z', state: 'Pending', conflict: false, fields: [{ field: 'Row', before: 'present', after: null }] },
]

export const GRADE_SEED: Grade[] = ['A3', 'A4', 'B1', 'B2', 'C1', 'C2', 'D1', 'D2', 'E1'].map((code, i) => ({ code, order: i + 1, retired: false }))

/** Admin settings defaults: TDD.md §9.2 password floors and lockout, GQ-03 idle timeout, REQ-OUT-03 masking, O-07 top 4 of 5. */
export const ADMIN_SETTINGS: AdminSettings = {
  analystMinPasswordLength: 10,
  adminMinPasswordLength: 14,
  lockoutAttempts: 5,
  lockoutMinutes: 15,
  idleTimeoutMinutes: 60,
  maskKeep: 2,
  maskCount: 6,
  analystsMayUnmask: true,
  topN: 5,
  emailsPreselected: 4,
  staleDataDays: 7,
  emailTurnaroundHours: 24,
  allowExternalProviders: false,
}

export const RESOURCE_TEAM: ResourceTeamMember[] = [
  { email: 'bench.team@example.com', displayName: 'Bench team', active: true, notes: 'Shared mailbox' },
  { email: 'resourcing.uk@example.com', displayName: 'UK resourcing', active: true, notes: '' },
]

export const DEFAULT_EMAIL_TEMPLATE = `Hi {candidate_name},

You have been shortlisted for {role} ({requisition}) with a match of {match_pct}.

Gaps to address:
{gaps}

Pointers for your CV:
{pointers}

Please update your CV against the attached JD and share it with {spoc_1} and {spoc_2}.

Kind regards`

export { EMAIL_PLACEHOLDERS } from '../../lib/emailTemplate'
