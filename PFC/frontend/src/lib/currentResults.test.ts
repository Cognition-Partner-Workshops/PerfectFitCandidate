import { describe, expect, it } from 'vitest'
import type { Band, MatchResult } from '../api/types'
import { summarizeCurrentResults } from './currentResults'

type ResultSummaryRow = Pick<
  MatchResult,
  'demandId' | 'band' | 'effectiveBand' | 'effectiveExcluded' | 'override'
>

const row = (
  demandId: number,
  band: Band,
  effectiveExcluded?: boolean,
): ResultSummaryRow => ({ demandId, band, effectiveExcluded })

describe('summarizeCurrentResults', () => {
  it('does not count effectively excluded results or use their bands', () => {
    const summaries = summarizeCurrentResults([
      row(1, 'Strong', true),
      row(1, 'Potential'),
      row(2, 'Weak', true),
    ])

    expect(summaries.get(1)).toEqual({ matches: 1, bestBand: 'Potential' })
    expect(summaries.has(2)).toBe(false)
  })
})
