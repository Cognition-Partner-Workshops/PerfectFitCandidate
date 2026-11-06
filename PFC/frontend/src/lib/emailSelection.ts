import type { MatchResult } from '../api/types'

export const DEFAULT_SHORTLIST = 5
export const DEFAULT_PRESELECTED = 4

/**
 * Default email suggestion: the top 4 of the top-5 shortlist per requisition are pre-selected
 * (REQ-EML-01, O-07). Excluded overrides are never pre-selected; Weak matches are only
 * suggested when nothing better exists, mirroring REQ-MAT-04.
 */
export function preselectForEmail(rows: MatchResult[], perRequisition = DEFAULT_PRESELECTED): Set<number> {
  const byReq = new Map<string, MatchResult[]>()
  for (const r of rows) {
    if (r.override?.action === 'exclude') continue
    const list = byReq.get(r.codeRequisition) ?? []
    list.push(r)
    byReq.set(r.codeRequisition, list)
  }
  const selected = new Set<number>()
  for (const list of byReq.values()) {
    const ranked = [...list].sort((a, b) => a.rank - b.rank).slice(0, DEFAULT_SHORTLIST)
    ranked.slice(0, perRequisition).forEach((r) => selected.add(r.resultId))
  }
  return selected
}

/** Group rows by requisition, preserving rank order. */
export function groupByRequisition<T extends { codeRequisition: string; rank: number }>(rows: T[]): [string, T[]][] {
  const map = new Map<string, T[]>()
  for (const r of rows) {
    const list = map.get(r.codeRequisition) ?? []
    list.push(r)
    map.set(r.codeRequisition, list)
  }
  return [...map.entries()].map(([k, v]) => [k, [...v].sort((a, b) => a.rank - b.rank)])
}

/** 24 h turnaround window from demand upload (REQ-EML-07). Negative = overdue. */
export function hoursLeft(uploadedAtIso: string, now: Date, targetHours = 24): number {
  const uploaded = new Date(uploadedAtIso).getTime()
  return Math.floor((uploaded + targetHours * 3600_000 - now.getTime()) / 3600_000)
}
