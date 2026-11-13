import type { Band, MatchResult } from '../api/types'
import { BAND_ORDER } from './bands'

export interface DemandMatchSummary {
  matches: number
  bestBand: Band | null
}

type ResultSummaryRow = Pick<
  MatchResult,
  'demandId' | 'band' | 'effectiveBand' | 'effectiveExcluded' | 'override'
>

export function effectiveResultBand(result: ResultSummaryRow): Band {
  return (
    result.effectiveBand ??
    (result.override?.action === 'adjust_band' && result.override.newBand
      ? result.override.newBand
      : result.band)
  )
}

export function summarizeCurrentResults(
  results: readonly ResultSummaryRow[],
): Map<number, DemandMatchSummary> {
  const summaries = new Map<number, DemandMatchSummary>()
  for (const result of results) {
    if (result.effectiveExcluded) continue
    const band = effectiveResultBand(result)
    const current = summaries.get(result.demandId)
    summaries.set(result.demandId, {
      matches: (current?.matches ?? 0) + 1,
      bestBand:
        current?.bestBand && BAND_ORDER.indexOf(current.bestBand) <= BAND_ORDER.indexOf(band)
          ? current.bestBand
          : band,
    })
  }
  return summaries
}
