/**
 * Illustrative mock-only eligibility and scoring so the SPA can be exercised without the backend.
 * It follows the baseline shape (REQ-ELIG-01..07, REQ-SCR-01/02, REQ-GRD-01) but is NOT the
 * scoring engine — the backend's deterministic scorer is authoritative.
 */
import type {
  Band,
  BenchStatus,
  CandidateType,
  Demand,
  Eligibility,
  LocationAssessment,
  MatchResult,
  Resource,
  ScoringTemplate,
  StatusMapEntry,
} from '../types'
import { bandFor } from '../../lib/bands'
import { GRADES } from '../../lib/analytics'
import { maskEmail } from '../../lib/mask'
import type { RawResource } from './fixtures'

export interface EligibilityResult {
  eligibility: Eligibility
  reasons: string[]
  priority: number | null
  unmapped: boolean
  caveat: string
}

export function evaluateEligibility(
  r: RawResource,
  statuses: BenchStatus[],
  map: StatusMapEntry[],
): EligibilityResult {
  const reasons: string[] = []
  const src = (r.benchStatus ?? '').trim().toLowerCase()
  const entry = map.find((m) => m.sourceText.trim().toLowerCase() === src)
  const status = entry ? statuses.find((s) => s.code === entry.canonicalCode) : undefined
  const unmapped = !status
  if (status?.eligibility === 'Excluded') reasons.push(`Status: ${status.label}`)
  if (r.groupName === 'Not on Bench') reasons.push('Group: Not on Bench')
  if (r.benchEndDate) reasons.push(`Bench End Date set (${r.benchEndDate})`)
  if (reasons.length) return { eligibility: 'excluded', reasons, priority: null, unmapped, caveat: '' }
  if (!r.grade) reasons.push('Grade not recorded')
  if (r.skills.length === 0) reasons.push('Skills insufficient')
  if (reasons.length) return { eligibility: 'insufficient', reasons, priority: null, unmapped, caveat: '' }
  // Unmapped statuses stay eligible at priority 2 with binary score 0 and a flag (GQ-04).
  if (unmapped) return { eligibility: 'eligible', reasons: [], priority: 2, unmapped: true, caveat: 'Unmapped status' }
  return {
    eligibility: status.eligibility === 'Eligible with caveat' ? 'eligible_caveat' : 'eligible',
    reasons: [],
    priority: status.priority,
    unmapped: false,
    caveat: status.caveat,
  }
}

export function classify(email: string | null, domains: string[]): CandidateType {
  if (!email) return 'Unknown'
  const domain = email.split('@')[1]?.toLowerCase() ?? ''
  return domains.includes(domain) ? 'Internal' : 'External'
}

export function toResource(
  r: RawResource,
  statuses: BenchStatus[],
  map: StatusMapEntry[],
  domains: string[],
): Resource {
  const e = evaluateEligibility(r, statuses, map)
  return {
    resourceId: r.resourceId,
    candidateId: r.resourceId,
    shortName: r.shortName,
    emailAddressMasked: maskEmail(r.email),
    name: r.name,
    grade: r.grade,
    location: r.location,
    benchStatus: r.benchStatus,
    benchStatusPriority: e.priority,
    unmappedStatus: e.unmapped,
    skills: r.skills,
    availableCapacity: r.availableCapacity,
    benchStartDate: r.benchStartDate,
    benchEndDate: r.benchEndDate,
    groupName: r.groupName,
    groupOwner: r.groupOwner,
    eligibility: e.eligibility,
    exclusionReasons: e.reasons,
    cvStatus: r.cvStatus,
    cvCount: r.cvCount,
    candidateType: classify(r.email, domains),
    identityStatus: r.identityStatus,
  }
}

const lc = (s: string) => s.toLowerCase()

function locationAssessment(region: string | null, location: string | null): LocationAssessment {
  if (!region || lc(region) === 'remote') return 'Remote/unspecified'
  if (location && lc(location).includes(lc(region))) return 'Aligned'
  return 'Potential travel'
}

function gradeDistance(a: string | null, b: string | null): number {
  const ia = GRADES.indexOf(a ?? '')
  const ib = GRADES.indexOf(b ?? '')
  return ia < 0 || ib < 0 ? 99 : ib - ia
}

/** Top-5 shortlist per scored demand; Weak rows only when nothing better exists (REQ-MAT-03/04). */
export function scoreAll(
  demands: Demand[],
  raws: RawResource[],
  resources: Resource[],
  template: ScoringTemplate,
  runId: number,
): MatchResult[] {
  const w = template.weights
  const out: MatchResult[] = []
  for (const d of demands.filter((x) => x.jdStatus === 'Scored')) {
    const rows: MatchResult[] = []
    for (const res of resources) {
      if (res.eligibility !== 'eligible' && res.eligibility !== 'eligible_caveat') continue
      const dist = gradeDistance(d.codeLocalGrade, res.grade)
      if (Math.abs(dist) > 1) continue
      const skills = res.skills.map(lc)
      const mandHit = d.mandatorySkills.filter((s) => skills.includes(lc(s)))
      const prefHit = d.preferredSkills.filter((s) => skills.includes(lc(s)))
      const mandRatio = d.mandatorySkills.length ? mandHit.length / d.mandatorySkills.length : 1
      const prefRatio = d.preferredSkills.length ? prefHit.length / d.preferredSkills.length : 1
      const skillPts = Math.round(w.skills * (0.7 * mandRatio + 0.3 * prefRatio))
      const gradePts = dist === 0 ? w.grade : template.adjacentGradePoints
      const loc = locationAssessment(d.region, res.location)
      const locPts = loc === 'Potential travel' ? Math.min(3, w.location) : w.location
      const startOk = !res.benchStartDate || !d.positionStartDate || res.benchStartDate <= d.positionStartDate
      const startPts = startOk ? w.benchStart : Math.round(w.benchStart / 2)
      const endPts = w.benchEnd
      const statusPts = res.unmappedStatus ? 0 : w.status
      const score = skillPts + gradePts + locPts + startPts + endPts + statusPts
      const missing = d.mandatorySkills.filter((s) => !skills.includes(lc(s)))
      const raw = raws.find((x) => x.resourceId === res.resourceId)
      const caveat = res.eligibility === 'eligible_caveat' ? `Status caveat: ${res.benchStatus}` : null
      const gaps = [
        ...missing.map((s) => `Mandatory gap: ${s}`),
        ...(dist !== 0 ? ['±1 grade consideration'] : []),
        ...(loc === 'Potential travel' ? ['Location: potential travel'] : []),
        ...(res.cvStatus === 'Not found' ? ['CV not found — CT skills only'] : []),
        ...(res.unmappedStatus ? ['Unmapped status'] : []),
        ...(caveat ? [caveat] : []),
        ...(res.identityStatus !== 'complete' ? ['Incomplete candidate information'] : []),
      ]
      rows.push({
        resultId: 0,
        runId,
        demandId: d.demandId,
        resourceId: res.resourceId,
        candidateId: res.candidateId,
        rank: 0,
        codeRequisition: d.codeRequisition,
        positionStartDate: d.positionStartDate,
        accountName: d.accountName,
        bu: d.bu,
        demandRequestName: d.demandRequestName,
        demandGrade: d.codeLocalGrade,
        region: d.region,
        personName: res.name,
        personEmailMasked: maskEmail(raw?.email ?? null),
        personGrade: res.grade,
        personLocation: res.location,
        benchStatus: res.benchStatus,
        score,
        band: bandFor(score, template.bands) as Band,
        gradeRelation: dist === 0 ? 'exact' : dist > 0 ? 'adjacent_above' : 'adjacent_below',
        locationAssessment: loc,
        matchingEvidence: [...mandHit, ...prefHit].map((s) => `${s} (CT skills)`),
        gapsChecks: gaps.length ? gaps : ['None'],
        eligibilityEvidence: [
          'Bench End Date blank',
          `Grade ${res.grade ?? '—'} within ±1 of ${d.codeLocalGrade ?? '—'}`,
          `Status ${res.benchStatus ?? 'Not recorded'}`,
        ],
        mandatoryMissing: missing.length,
        candidateType: res.candidateType,
        incompleteIdentity: res.identityStatus !== 'complete',
        emailPreselected: false,
        breakdown: [
          { component: 'skills', points: skillPts, maxPoints: w.skills },
          { component: 'grade', points: gradePts, maxPoints: w.grade },
          { component: 'location', points: locPts, maxPoints: w.location },
          { component: 'bench_start', points: startPts, maxPoints: w.benchStart },
          { component: 'bench_end', points: endPts, maxPoints: w.benchEnd },
          { component: 'status', points: statusPts, maxPoints: w.status },
        ],
      })
    }
    rows.sort((a, b) => b.score - a.score || (a.benchStatus ?? '').localeCompare(b.benchStatus ?? ''))
    const hasSuitable = rows.some((r) => r.band !== 'Weak')
    const shortlist = (hasSuitable ? rows.filter((r) => r.band !== 'Weak') : rows).slice(0, 5)
    shortlist.forEach((r, i) => {
      r.resultId = r.demandId * 10_000 + r.resourceId
      r.rank = i + 1
      r.emailPreselected = i < 4
      out.push(r)
    })
  }
  return out
}
