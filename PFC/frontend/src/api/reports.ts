import type { AnalyticsSummary, DemandAnalytics, BenchAnalytics, MatchAnalytics, CountItem, DashboardFilters, ResultFilters } from './types'

export interface DashboardAnalytics {
  summary: AnalyticsSummary
  demand: DemandAnalytics
  bench: BenchAnalytics
  match: MatchAnalytics
  extended: ExtendedAnalytics
}

export interface MatrixCell { row: string; column: string; count: number }
export interface AnalyticsPerson { resourceId: number; candidateId: number; name: string | null; emailMasked: string | null }
export interface ExtendedAnalytics {
  gradeMatrix: (MatrixCell & { demandCount: number; benchCount: number; relation: 'exact' | 'adjacent' | 'outside' })[]
  scoreHistogram: { thresholds: number[]; bins: { lower: number; upper: number; allPairs: number; top5: number }[] }
  shortlistFunnel: CountItem[]
  unmatchedDemands: { demandId: number; codeRequisition: string; positionStartDate: string | null; reasonCodes: string[] }[]
  emailTrend: { week: string; sentCount: number; medianHours: number; within24hPct: number }[]
  readinessStatus: MatrixCell[]
  locationMatrix: MatrixCell[]
  skillCooccurrence: MatrixCell[]
  upskilling: (AnalyticsPerson & { matchedSkills: string[]; missingSkills: string[]; bestScore: number | null })[]
  accuracy: { runId: number; confirmedDemands: number; confirmedPairs: number; hits: number; hitRateAt5: number | null; precisionAt5: number | null }[]
  exclusionReasons: CountItem[]
  candidateTypes: CountItem[]
  contention: (AnalyticsPerson & { demandCount: number; requisitions: string[] })[]
  demandAging: { buckets: CountItem[]; unknownStart: number; pastStartWithoutMatch: number }
  overrideRates: { runId: number; pairCount: number; overriddenPairs: number; ratePct: number; byType: CountItem[] }[]
}

export type PdfSection = 'cover' | 'summary' | 'demand' | 'bench' | 'match' | 'unmatched' | 'extended-other' | 'data-quality-method' | 'match-results'
export interface PdfRequest {
  report: 'dashboard' | 'match_results'
  scope: 'filters' | 'all'
  sections: PdfSection[]
  orientation: 'portrait' | 'landscape'
  filters?: DashboardFilters
  resultFilters?: ResultsQuery
}
export type ResultsQuery = Omit<ResultFilters, 'codeRequisition' | 'accountName' | 'location'> & {
  requisition?: string[]; account?: string[]; personLocation?: string[]
  sort: 'rank' | 'score'; descending: boolean
}

export function emptyExtendedAnalytics(): ExtendedAnalytics {
  return { gradeMatrix: [], scoreHistogram: { thresholds: [61, 70, 90], bins: [] }, shortlistFunnel: [], unmatchedDemands: [], emailTrend: [], readinessStatus: [], locationMatrix: [], skillCooccurrence: [], upskilling: [], accuracy: [], exclusionReasons: [], candidateTypes: [], contention: [], demandAging: { buckets: [], unknownStart: 0, pastStartWithoutMatch: 0 }, overrideRates: [] }
}
