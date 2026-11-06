import type { MatchResult } from '../api/types'
import { bandLabel } from './bands'
import { formatDate, roleLocation } from './format'

/** The 16 data columns in exact REQ-OUT-01 order (also used for exports, REQ-OUT-09). */
export const RESULT_COLUMNS = [
  'Requisition',
  'Start Date',
  'Account',
  'Role',
  'Demand Grade',
  'Role Location',
  'Potential Person',
  'Person Email',
  'Person Grade',
  'Person Location',
  'Match %',
  'Match Band',
  'Location Assessment',
  'Matching Evidence',
  'Gaps / Checks',
  'Eligibility Evidence',
] as const

export const DISCLAIMER = 'Scores are relative matching metrics for human review, not hiring decisions.'

function cell(value: string): string {
  const v = /^[=+\-@]/.test(value) ? `'${value}` : value
  return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v
}

/** CSV of the current filtered view with masked emails, run metadata and disclaimer. */
export function resultsToCsv(rows: MatchResult[], meta: string): string {
  const lines = [cell(meta), cell(DISCLAIMER), RESULT_COLUMNS.join(',')]
  for (const r of rows) {
    lines.push(
      [
        r.codeRequisition,
        formatDate(r.positionStartDate),
        r.accountName ?? '',
        r.demandRequestName ?? '',
        r.demandGrade ?? '',
        roleLocation(r.region),
        r.personName ?? '',
        r.personEmailMasked ?? '',
        r.personGrade ?? '',
        r.personLocation ?? '',
        String(Math.round(r.score)),
        bandLabel(r.band),
        r.locationAssessment,
        r.matchingEvidence.join('; '),
        r.gapsChecks.join('; '),
        r.eligibilityEvidence.join('; '),
      ]
        .map(cell)
        .join(','),
    )
  }
  return lines.join('\n')
}
