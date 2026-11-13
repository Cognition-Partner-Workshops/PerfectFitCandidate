import type {
  AnalyticsSummary,
  BenchAnalytics,
  CountItem,
  Demand,
  DemandAnalytics,
  EmailDraft,
  MatchAnalytics,
  MatchResult,
  MatchRun,
  Resource,
  ValidationCheck,
} from '../api/types'
import { BAND_ORDER } from './bands'

export const GRADES = ['A3', 'A4', 'B1', 'B2', 'C1', 'C2', 'D1', 'D2', 'E1']

function topN(counts: Map<string, number>, n: number): CountItem[] {
  return [...counts.entries()]
    .map(([label, count]) => ({ label, count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
    .slice(0, n)
}

function inc(map: Map<string, number>, key: string, by = 1): void {
  map.set(key, (map.get(key) ?? 0) + by)
}

const isEligible = (r: Resource) => r.eligibility === 'eligible' || r.eligibility === 'eligible_caveat'

/** Dashboard header (REQ-OUT-04). Weak-only requisitions count as "no suitable candidate" (GQ-01). */
export function summarise(
  demands: Demand[],
  resources: Resource[],
  results: MatchResult[],
  emails: EmailDraft[],
  run: MatchRun | null,
): AnalyticsSummary {
  const byReq = new Map<string, MatchResult[]>()
  results.forEach((r) => byReq.set(r.codeRequisition, [...(byReq.get(r.codeRequisition) ?? []), r]))
  const scoredReqs = demands.filter((d) => d.jdStatus === 'Confirmed')
  let noSuitable = 0
  let weakOnly = 0
  let covered = 0
  for (const d of scoredReqs) {
    const rows = byReq.get(d.codeRequisition) ?? []
    const suitable = rows.some((r) => r.band === 'Strong' || r.band === 'Potential' || r.band === 'Avg')
    if (!suitable) noSuitable += 1
    if (!suitable && rows.some((r) => r.band === 'Weak')) weakOnly += 1
    if (rows.some((r) => r.band === 'Strong' || r.band === 'Potential')) covered += 1
  }
  const dlIssues = demands.filter((d) => !d.idFormatOk || !d.codeLocalGrade).length
  const ctIssues = resources.filter(
    (r) => !r.grade || !r.emailAddressMasked || r.unmappedStatus || r.cvStatus === 'Not found' || r.identityStatus !== 'complete',
  ).length
  const sent = emails.filter((e) => e.status === 'Sent' && e.sentAt)
  const within = sent.filter(
    (e) => new Date(e.sentAt ?? '').getTime() - new Date(e.demandUploadedAt).getTime() <= 24 * 3600_000,
  )
  return {
    run,
    runsIncluded: new Set(results.map((r) => r.runId)).size,
    requisitionsAssessed: scoredReqs.length,
    openRequisitions: demands.length,
    roleToPersonCombinations: results.length,
    rolesWithNoSuitableCandidate: noSuitable,
    weakOnlyRequisitions: weakOnly,
    dataQualityIssues: { total: dlIssues + ctIssues, dl: dlIssues, ct: ctIssues },
    eligibleBench: resources.filter(isEligible).length,
    excludedBench: resources.filter((r) => !isEligible(r)).length,
    matchCoveragePct: scoredReqs.length ? Math.round((covered / scoredReqs.length) * 100) : 0,
    emailsWithin24hPct: sent.length ? Math.round((within.length / sent.length) * 100) : 0,
  }
}

function byDateDesc<T>(rows: T[], pick: (r: T) => string | null): T[] {
  return [...rows].sort((a, b) => (pick(b) ?? '').localeCompare(pick(a) ?? ''))
}

/** Demand analytics (REQ-OUT-05). */
export function demandAnalytics(demands: Demand[]): DemandAnalytics {
  const buRegion = new Map<string, number>()
  const skills = new Map<string, number>()
  const grades = new Map<string, number>()
  const accounts = new Map<string, number>()
  const pipeline = new Map<string, number>()
  for (const d of demands) {
    inc(buRegion, `${d.bu ?? 'Unspecified'}|${d.region ?? 'Unspecified'}`)
    new Set([...d.mandatorySkills, ...d.preferredSkills]).forEach((s) => inc(skills, s))
    inc(grades, d.codeLocalGrade ?? 'Not recorded')
    inc(accounts, d.accountName ?? 'Not recorded')
    if (d.positionStartDate) inc(pipeline, d.positionStartDate.slice(0, 7))
  }
  return {
    openByBuRegion: [...buRegion.entries()].map(([k, count]) => {
      const [bu = '', region = ''] = k.split('|')
      return { bu, region, count }
    }),
    topSkills: topN(skills, 5),
    gradeToJdCount: GRADES.map((g) => ({ label: g, count: grades.get(g) ?? 0 })),
    topAccounts: topN(accounts, 3),
    startPipeline: [...pipeline.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([label, count]) => ({ label, count })),
    rows: byDateDesc(demands, (d) => d.positionStartDate),
  }
}

const AGING_BUCKETS: [string, number][] = [
  ['0–2 wks', 14],
  ['2–4 wks', 28],
  ['4–8 wks', 56],
  ['8–12 wks', 84],
  ['> 12 wks', Number.POSITIVE_INFINITY],
]

/** Bench analytics (REQ-OUT-06). Available Capacity is a display-only split, never availability (REQ-ELIG-06). */
export function benchAnalytics(resources: Resource[], demands: Demand[], today: Date): BenchAnalytics {
  const eligible = resources.filter(isEligible)
  const poolSkills = new Map<string, number>()
  eligible.forEach((r) => new Set(r.skills).forEach((s) => inc(poolSkills, s)))
  const demanded = demandAnalytics(demands).topSkills
  const lc = (s: string) => s.toLowerCase()
  const demandVsSupply = demanded.map((s) => {
    const holders = eligible.filter((r) => r.skills.some((k) => lc(k) === lc(s.label)))
    return {
      skill: s.label,
      demandCount: s.count,
      supplyY: holders.filter((r) => r.availableCapacity === 'Y').length,
      supplyN: holders.filter((r) => r.availableCapacity !== 'Y').length,
    }
  })
  const todayIso = today.toISOString().slice(0, 10)
  const days = (from: string, to: string) =>
    Math.max(0, Math.round((new Date(to).getTime() - new Date(from).getTime()) / 86_400_000))
  const timeline = eligible
    .filter((r) => r.benchStartDate)
    .map((r) => {
      const start = r.benchStartDate ?? todayIso
      const end = r.benchEndDate ?? todayIso
      return { name: r.name ?? r.shortName, start, end, days: days(start, end) }
    })
  const aging = AGING_BUCKETS.map(([label]) => ({ label, count: 0 }))
  for (const t of timeline) {
    const idx = AGING_BUCKETS.findIndex(([, max]) => t.days < max)
    const bucket = aging[idx]
    if (bucket) bucket.count += 1
  }
  const statusMap = new Map<string, { count: number; excluded: boolean; priority: number }>()
  for (const r of resources) {
    const key = r.benchStatus ?? 'Not recorded'
    const prev = statusMap.get(key)
    statusMap.set(key, {
      count: (prev?.count ?? 0) + 1,
      excluded: !isEligible(r),
      priority: r.benchStatusPriority ?? 99,
    })
  }
  return {
    topSkills: topN(poolSkills, 5),
    demandVsSupply,
    gradeHistogram: GRADES.map((g) => ({
      grade: g,
      bench: eligible.filter((r) => r.grade === g).length,
      demand: demands.filter((d) => d.codeLocalGrade === g).length,
    })),
    timeline,
    aging,
    statusDistribution: [...statusMap.entries()]
      .sort(([, a], [, b]) => Number(a.excluded) - Number(b.excluded) || a.priority - b.priority)
      .map(([status, v]) => ({ status, count: v.count, excluded: v.excluded })),
    rows: byDateDesc(resources, (r) => r.benchStartDate),
  }
}

/** Match/communication analytics (REQ-OUT-07) and validation panel (REQ-OUT-10). */
export function matchAnalytics(
  summary: AnalyticsSummary,
  results: MatchResult[],
  emails: EmailDraft[],
  validation: ValidationCheck[],
): MatchAnalytics {
  return {
    coveragePct: summary.matchCoveragePct,
    bandDistribution: BAND_ORDER.map((b) => ({ label: b, count: results.filter((r) => r.band === b).length })),
    emailStatusCounts: (['Draft', 'Approved', 'Sent', 'Failed'] as const).map((s) => ({
      label: s,
      count: emails.filter((e) => e.status === s).length,
    })),
    validation,
  }
}
