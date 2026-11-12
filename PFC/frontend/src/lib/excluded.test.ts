import { describe, expect, it } from 'vitest'
import type { ExcludedEntry, Resource } from '../api/types'
import { eligibilityReasonLabel, eligibilityReasonTitle, groupExcluded } from './excluded'

const entry = (o: Partial<ExcludedEntry>): ExcludedEntry => ({
  runId: 4, resourceId: 1, shortName: 'Alex', demandId: null, codeRequisition: null, reasonCode: 'NO_GRADE', detail: 'Person grade not recorded or unknown', scope: 'global', ...o,
})
const bench = { resourceId: 1, candidateId: 7, name: null, emailAddressMasked: 'a***@example.com', grade: 'Z9', benchStatus: 'Available' } as Resource

describe('groupExcluded (REQ-ELIG-08)', () => {
  it('groups reasons per person and scope, labels codes and joins bench data', () => {
    const rows = groupExcluded(
      [
        entry({}),
        entry({ reasonCode: 'NO_SKILLS', detail: 'No recognised skills recorded in CT or CV' }),
        entry({ scope: 'pair', demandId: 3, codeRequisition: 'T1', reasonCode: 'GRADE_GAP_GT1', detail: 'Grade outside ±1' }),
        entry({ resourceId: 2, shortName: 'Sam', reasonCode: 'CUSTOM', detail: null }),
      ],
      [bench],
    )
    expect(rows.map((r) => [r.shortName, r.codeRequisition, r.reasons.map((x) => x.label)])).toEqual([
      ['Alex', null, ['Grade unknown', 'No skills in dictionary (0 unrecognised)']],
      ['Alex', 'T1', ['Grade outside ±1']],
      ['Sam', null, ['CUSTOM']],
    ])
    expect(rows[0]).toMatchObject({ candidateId: 7, emailAddressMasked: 'a***@example.com', grade: 'Z9', benchStatus: 'Available' })
    expect(rows[2]).toMatchObject({ candidateId: 0, grade: null, emailAddressMasked: null })
  })
})

describe('NO_SKILLS label', () => {
  it('shows the unrecognised count and lists safe terms in its tooltip', () => {
    expect(eligibilityReasonLabel('NO_SKILLS', ['Kubernetes', 'Product Management'])).toBe(
      'No skills in dictionary (2 unrecognised)',
    )
    expect(eligibilityReasonTitle('NO_SKILLS', ['Kubernetes', 'Product Management'])).toBe(
      'Unrecognised skills: Kubernetes, Product Management',
    )
  })
})
