import type { MatchResult, ResultFilters } from '../api/types'

function eq(a: string | null | undefined, b: string | undefined): boolean {
  return !b || (a ?? '').toLowerCase() === b.toLowerCase()
}

function contains(a: string | null | undefined, b: string | undefined): boolean {
  return !b || (a ?? '').toLowerCase().includes(b.trim().toLowerCase())
}

/**
 * Match Results filters (REQ-OUT-08, REQ-MAT-10): Code Requisition, Account Name, BU, Grade
 * (demand and person), Location, region, band, status, candidate type, incomplete identity,
 * mandatory-skill status and score range.
 */
export function applyResultFilters(rows: MatchResult[], f: ResultFilters): MatchResult[] {
  return rows.filter(
    (r) =>
      contains(r.codeRequisition, f.codeRequisition) &&
      eq(r.accountName, f.accountName) &&
      eq(r.bu, f.bu) &&
      eq(r.demandGrade, f.demandGrade) &&
      eq(r.personGrade, f.personGrade) &&
      eq(r.personLocation, f.location) &&
      eq(r.region, f.region) &&
      (!f.band || r.band === f.band) &&
      eq(r.benchStatus, f.status) &&
      (!f.candidateType || r.candidateType === f.candidateType) &&
      (!f.incompleteIdentity || r.incompleteIdentity === (f.incompleteIdentity === 'yes')) &&
      (!f.mandatorySkills || (f.mandatorySkills === 'gap' ? r.mandatoryMissing > 0 : r.mandatoryMissing === 0)) &&
      (f.scoreMin === undefined || r.score >= f.scoreMin) &&
      (f.scoreMax === undefined || r.score <= f.scoreMax),
  )
}

export function distinct<T>(rows: T[], pick: (row: T) => string | null | undefined): string[] {
  const set = new Set<string>()
  for (const r of rows) {
    const v = pick(r)
    if (v) set.add(v)
  }
  return [...set].sort((a, b) => a.localeCompare(b))
}

export function activeFilterCount(f: object): number {
  return Object.values(f).filter((v) => v !== undefined && v !== '').length
}
