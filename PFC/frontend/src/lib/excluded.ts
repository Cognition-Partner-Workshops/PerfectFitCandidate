import type { ExcludedEntry, Resource } from '../api/types'

/** Labels for the backend's eligibility reason codes (pfc/domain/scoring/engine.py, REQ-ELIG-01..08). */
export const REASON_LABELS: Record<string, string> = {
  STATUS_EXCLUDED: 'Excluded bench status',
  NOT_ON_BENCH: 'Not on bench',
  BENCH_END_SET: 'Bench end date set',
  NO_GRADE: 'Grade unknown',
  NO_SKILLS: 'No recognised skills',
  GRADE_GAP_GT1: 'Grade outside ±1',
}

export interface ExcludedPerson {
  key: string
  resourceId: number
  candidateId: number
  shortName: string
  name: string | null
  emailAddressMasked: string | null
  grade: string | null
  benchStatus: string | null
  /** null = excluded from the whole run; otherwise excluded for this requisition only. */
  codeRequisition: string | null
  reasons: { code: string; label: string; detail: string | null }[]
}

/** One row per person and scope, joined to the bench row for name, masked email, grade and status. */
export function groupExcluded(entries: ExcludedEntry[], resources: Resource[]): ExcludedPerson[] {
  const byId = new Map(resources.map((r) => [r.resourceId, r]))
  const rows = new Map<string, ExcludedPerson>()
  for (const e of entries) {
    const req = e.scope === 'pair' ? e.codeRequisition : null
    const key = `${e.resourceId}|${req ?? ''}`
    let row = rows.get(key)
    if (!row) {
      const r = byId.get(e.resourceId)
      row = {
        key,
        resourceId: e.resourceId,
        candidateId: r?.candidateId ?? 0,
        shortName: e.shortName,
        name: r?.name ?? null,
        emailAddressMasked: r?.emailAddressMasked ?? null,
        grade: r?.grade ?? null,
        benchStatus: r?.benchStatus ?? null,
        codeRequisition: req,
        reasons: [],
      }
      rows.set(key, row)
    }
    if (!row.reasons.some((x) => x.code === e.reasonCode && x.detail === e.detail))
      row.reasons.push({ code: e.reasonCode, label: REASON_LABELS[e.reasonCode] ?? e.detail ?? e.reasonCode, detail: e.detail })
  }
  return [...rows.values()]
}
