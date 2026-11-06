import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import type { EChartsCoreOption } from 'echarts/core'
import { api } from '../api'
import { toDashboardQuery } from '../api/mappers'
import type { DashboardFilters, ResultFilters } from '../api/types'
import { EChart } from '../components/EChart'
import { CHART_PALETTE } from '../lib/chartPalette'
import { FilterBar } from '../components/FilterBar'
import { Banner, BandChip, ChartCard, EligibilityBadge, ErrorState, KpiTile, Spinner } from '../components/ui'
import { formatDate, formatDateTime, roleLocation } from '../lib/format'
import { distinct } from '../lib/filters'
import { bandColorVar, bandLabel } from '../lib/bands'
import type { Band } from '../api/types'

const BAND_HEX: Record<Band, string> = {
  Strong: '#067647',
  Potential: '#0b5cad',
  Avg: '#b54708',
  Weak: '#b42318',
  'Insufficient data': '#667085',
}

const axisLabel = { color: '#475467', fontSize: 11 }
const grid = { left: 8, right: 16, top: 28, bottom: 8, containLabel: true }

function hbar(labels: string[], values: number[], name: string): EChartsCoreOption {
  return {
    tooltip: { trigger: 'axis' },
    grid,
    xAxis: { type: 'value', axisLabel },
    yAxis: { type: 'category', data: [...labels].reverse(), axisLabel },
    series: [{ name, type: 'bar', data: [...values].reverse(), barMaxWidth: 18, itemStyle: { borderRadius: [0, 4, 4, 0] }, label: { show: true, position: 'right' } }],
  }
}

function vbar(labels: string[], series: { name: string; data: number[]; stack?: string }[]): EChartsCoreOption {
  return {
    tooltip: { trigger: 'axis' },
    legend: series.length > 1 ? { top: 0, textStyle: axisLabel } : undefined,
    grid,
    xAxis: { type: 'category', data: labels, axisLabel },
    yAxis: { type: 'value', axisLabel, minInterval: 1 },
    series: series.map((s) => ({ ...s, type: 'bar', barMaxWidth: 28, itemStyle: { borderRadius: s.stack ? 0 : [4, 4, 0, 0] } })),
  }
}

type Section = 'overview' | 'demand' | 'bench' | 'match' | 'quality'

const SECTIONS: { id: Section; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'demand', label: 'Demand analytics' },
  { id: 'bench', label: 'Bench analytics' },
  { id: 'match', label: 'Match & communication' },
  { id: 'quality', label: 'Data quality' },
]

/** Dashboard landing (REQ-OUT-04..08, GUI_OCC_DESIGN.md §8). Charts use Apache ECharts (DASHBOARD_RESEARCH.md §7). */
export function DashboardPage() {
  const [filters, setFilters] = useState<ResultFilters>({})
  const [section, setSection] = useState<Section>('overview')
  const df: DashboardFilters = useMemo(() => toDashboardQuery(filters), [filters])
  const summary = useQuery({ queryKey: ['analytics', 'summary', df], queryFn: () => api.analyticsSummary(df) })
  const demand = useQuery({ queryKey: ['analytics', 'demand', df], queryFn: () => api.analyticsDemand(df) })
  const bench = useQuery({ queryKey: ['analytics', 'bench', df], queryFn: () => api.analyticsBench(df) })
  const match = useQuery({ queryKey: ['analytics', 'match', df], queryFn: () => api.analyticsMatch(df) })
  const all = useQuery({ queryKey: ['analytics', 'options'], queryFn: () => Promise.all([api.listDemands(), api.listResources()]) })

  const options = useMemo(() => {
    const [ds = [], rs = []] = all.data ?? []
    return {
      accounts: distinct(ds, (d) => d.accountName),
      bus: distinct(ds, (d) => d.bu),
      grades: ['A3', 'A4', 'B1', 'B2', 'C1', 'C2', 'D1', 'D2', 'E1'],
      locations: distinct(rs, (r) => r.location),
      regions: distinct(ds, (d) => d.region),
      statuses: distinct(rs, (r) => r.benchStatus),
    }
  }, [all.data])

  if (summary.isError) return <div className="page"><ErrorState error={summary.error} onRetry={() => void summary.refetch()} /></div>
  const s = summary.data
  const show = (id: Section) => section === 'overview' || section === id

  return (
    <div className="page">
      <div className="page-header">
        <h1>Dashboard</h1>
        <span className="spacer" />
        {s?.run && (
          <span className="meta">
            Run: {formatDateTime(s.run.finishedAt)} · Template v{s.run.templateVersion}
          </span>
        )}
      </div>
      <div className="card">
        <FilterBar value={filters} onChange={setFilters} options={options} />
      </div>
      {!s ? (
        <Spinner />
      ) : (
        <>
          <div className="kpi-row">
            <KpiTile label="Requisitions assessed" value={s.requisitionsAssessed} hint={`of ${s.openRequisitions} open`} />
            <KpiTile label="Role-to-person combinations" value={s.roleToPersonCombinations} hint="top 5 per requisition" accent="var(--color-chart-3)" />
            <KpiTile
              label="Roles with no suitable candidate"
              value={s.rolesWithNoSuitableCandidate}
              hint={`${s.requisitionsAssessed ? ((s.rolesWithNoSuitableCandidate / s.requisitionsAssessed) * 100).toFixed(1) : '0.0'} % of reqs · ${s.weakOnlyRequisitions} Weak-only`}
              accent="var(--color-warning)"
            />
            <KpiTile
              label="Data-quality limitations"
              value={<>{s.dataQualityIssues.total} <span className="small">⚠</span></>}
              hint={`DL ${s.dataQualityIssues.dl} · CT ${s.dataQualityIssues.ct}`}
              accent="var(--color-danger)"
            />
          </div>
          <div className="strip">
            <span>Eligible bench <strong>{s.eligibleBench}</strong></span>
            <span>Excluded <strong>{s.excludedBench}</strong></span>
            <span>Match coverage <strong>{s.matchCoveragePct} %</strong></span>
            <span>Emails sent ≤ 24 h <strong>{s.emailsWithin24hPct} %</strong></span>
          </div>
        </>
      )}
      <div className="tabs" role="tablist" aria-label="Dashboard sections">
        {SECTIONS.map((t) => (
          <button key={t.id} role="tab" aria-selected={section === t.id} onClick={() => setSection(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      {show('demand') && demand.data && (
        <div className="grid-12" aria-label="Demand analytics">
          <ChartCard
            title="Open demands by BU / region"
            subtitle="Stacked by region"
            table={{ columns: ['BU', 'Region', 'Demands'], rows: demand.data.openByBuRegion.map((r) => [r.bu, r.region, r.count]) }}
          >
            {(() => {
              const bus = [...new Set(demand.data.openByBuRegion.map((r) => r.bu))]
              const regions = [...new Set(demand.data.openByBuRegion.map((r) => r.region))]
              return (
                <EChart
                  ariaLabel="Open demands by BU and region"
                  onSelect={(bu) => setFilters((f) => ({ ...f, bu }))}
                  option={vbar(
                    bus,
                    regions.map((rg) => ({
                      name: rg,
                      stack: 'r',
                      data: bus.map((b) => demand.data.openByBuRegion.find((x) => x.bu === b && x.region === rg)?.count ?? 0),
                    })),
                  )}
                />
              )
            })()}
          </ChartCard>
          <ChartCard
            title="Top 5 skills in demand"
            subtitle="Synonym-normalised"
            table={{ columns: ['Skill', 'Demands'], rows: demand.data.topSkills.map((x) => [x.label, x.count]) }}
          >
            <EChart ariaLabel="Top 5 skills in demand" option={hbar(demand.data.topSkills.map((x) => x.label), demand.data.topSkills.map((x) => x.count), 'Demands')} />
          </ChartCard>
          <ChartCard
            title="Grade → JD count"
            subtitle="Open demands per Code Local Grade"
            table={{ columns: ['Grade', 'JDs'], rows: demand.data.gradeToJdCount.map((x) => [x.label, x.count]) }}
          >
            <EChart
              ariaLabel="Grade to JD count"
              onSelect={(g) => setFilters((f) => ({ ...f, demandGrade: g }))}
              option={vbar(demand.data.gradeToJdCount.map((x) => x.label), [{ name: 'JDs', data: demand.data.gradeToJdCount.map((x) => x.count) }])}
            />
          </ChartCard>
          <ChartCard title="Top 3 accounts by JD count" table={{ columns: ['Account', 'JDs'], rows: demand.data.topAccounts.map((x) => [x.label, x.count]) }}>
            <EChart ariaLabel="Top 3 accounts" onSelect={(a) => setFilters((f) => ({ ...f, accountName: a }))} option={hbar(demand.data.topAccounts.map((x) => x.label), demand.data.topAccounts.map((x) => x.count), 'JDs')} />
          </ChartCard>
          <ChartCard title="Demand start-date pipeline" subtitle="By month" table={{ columns: ['Month', 'Demands'], rows: demand.data.startPipeline.map((x) => [x.label, x.count]) }} span={8}>
            <EChart ariaLabel="Demand start-date pipeline" option={vbar(demand.data.startPipeline.map((x) => x.label), [{ name: 'Demands', data: demand.data.startPipeline.map((x) => x.count) }])} />
          </ChartCard>
          <section className="card span-12" aria-label="Demand List rows">
            <div className="card-header">
              <h3>Demand List</h3>
              <span className="sub">Ordered by Position Start Date (newest first)</span>
            </div>
            <div className="table-wrap scroll">
              <table className="data">
                <thead>
                  <tr>
                    <th>Code Requisition</th>
                    <th>Position Start Date</th>
                    <th>Account Name</th>
                    <th>Grade</th>
                    <th>Demand Request Name</th>
                    <th>Region</th>
                    <th>BU</th>
                    <th>JD status</th>
                    <th>Best band</th>
                  </tr>
                </thead>
                <tbody>
                  {demand.data.rows.map((d) => (
                    <tr key={d.demandId}>
                      <td className="mono">{d.codeRequisition}</td>
                      <td>{formatDate(d.positionStartDate)}</td>
                      <td>{d.accountName}</td>
                      <td>{d.codeLocalGrade}</td>
                      <td>{d.demandRequestName}</td>
                      <td>{roleLocation(d.region)}</td>
                      <td>{d.bu}</td>
                      <td>{d.jdStatus}</td>
                      <td>{d.bestBand ? <BandChip band={d.bestBand} /> : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {show('bench') && bench.data && (
        <div className="grid-12" aria-label="Bench analytics">
          <ChartCard title="Top 5 skills in pool" subtitle="Eligible bench" table={{ columns: ['Skill', 'People'], rows: bench.data.topSkills.map((x) => [x.label, x.count]) }}>
            <EChart ariaLabel="Top 5 skills in pool" option={hbar(bench.data.topSkills.map((x) => x.label), bench.data.topSkills.map((x) => x.count), 'People')} />
          </ChartCard>
          <ChartCard
            title="Top 5 demanded skills vs eligible bench supply"
            subtitle="Stacked by Available Capacity Y/N — display only, not availability"
            table={{
              columns: ['Skill', 'Demands', 'Supply (Capacity Y)', 'Supply (Capacity N/blank)'],
              rows: bench.data.demandVsSupply.map((x) => [x.skill, x.demandCount, x.supplyY, x.supplyN]),
            }}
            span={8}
          >
            <EChart
              ariaLabel="Top 5 demanded skills versus eligible bench supply"
              option={{
                tooltip: { trigger: 'axis' },
                legend: { top: 0, textStyle: axisLabel },
                grid,
                yAxis: { type: 'category', data: bench.data.demandVsSupply.map((x) => x.skill).reverse(), axisLabel },
                xAxis: { type: 'value', axisLabel, minInterval: 1 },
                series: [
                  { name: 'Demands', type: 'bar', data: bench.data.demandVsSupply.map((x) => x.demandCount).reverse(), barMaxWidth: 14, itemStyle: { color: CHART_PALETTE[6] } },
                  { name: 'Supply · Capacity Y', type: 'bar', stack: 'supply', data: bench.data.demandVsSupply.map((x) => x.supplyY).reverse(), barMaxWidth: 14, itemStyle: { color: CHART_PALETTE[0] } },
                  { name: 'Supply · Capacity N', type: 'bar', stack: 'supply', data: bench.data.demandVsSupply.map((x) => x.supplyN).reverse(), barMaxWidth: 14, itemStyle: { color: CHART_PALETTE[4] } },
                ],
              }}
            />
          </ChartCard>
          <ChartCard
            title="Grade histogram"
            subtitle="Eligible bench vs open demand (A3 → E1)"
            table={{ columns: ['Grade', 'Bench', 'Demand'], rows: bench.data.gradeHistogram.map((x) => [x.grade, x.bench, x.demand]) }}
          >
            <EChart
              ariaLabel="Grade histogram"
              option={vbar(bench.data.gradeHistogram.map((x) => x.grade), [
                { name: 'Bench', data: bench.data.gradeHistogram.map((x) => x.bench) },
                { name: 'Demand', data: bench.data.gradeHistogram.map((x) => x.demand) },
              ])}
            />
          </ChartCard>
          <ChartCard
            title="Bench timeline"
            subtitle="Bench Start → today; bubble size = days on bench"
            table={{ columns: ['Person', 'Bench Start', 'Days'], rows: bench.data.timeline.map((x) => [x.name, formatDate(x.start), x.days]) }}
          >
            <EChart
              ariaLabel="Bench timeline bubble chart"
              option={{
                tooltip: { formatter: (p: { data: [string, string, number] }) => `${p.data[1]}: ${p.data[2]} days` },
                grid,
                xAxis: { type: 'time', axisLabel },
                yAxis: { type: 'category', data: bench.data.timeline.map((x) => x.name), axisLabel: { ...axisLabel, fontSize: 10 } },
                series: [
                  {
                    type: 'scatter',
                    data: bench.data.timeline.map((x) => [x.start, x.name, x.days]),
                    symbolSize: (v: [string, string, number]) => Math.max(8, Math.min(30, v[2] / 4)),
                    itemStyle: { color: CHART_PALETTE[0], opacity: 0.7 },
                  },
                ],
              }}
            />
          </ChartCard>
          <ChartCard title="Bench aging" table={{ columns: ['Weeks on bench', 'People'], rows: bench.data.aging.map((x) => [x.label, x.count]) }}>
            <EChart ariaLabel="Bench aging" option={vbar(bench.data.aging.map((x) => x.label), [{ name: 'People', data: bench.data.aging.map((x) => x.count) }])} />
          </ChartCard>
          <ChartCard
            title="Bench status distribution"
            subtitle="Eligible statuses in priority order, then excluded"
            table={{ columns: ['Status', 'People', 'Excluded'], rows: bench.data.statusDistribution.map((x) => [x.status, x.count, x.excluded ? 'Yes' : 'No']) }}
            span={8}
          >
            <EChart
              ariaLabel="Bench status distribution"
              onSelect={(st) => setFilters((f) => ({ ...f, status: st }))}
              option={{
                tooltip: { trigger: 'axis' },
                grid,
                xAxis: { type: 'value', axisLabel, minInterval: 1 },
                yAxis: { type: 'category', data: bench.data.statusDistribution.map((x) => (x.excluded ? `${x.status} (excluded)` : x.status)).reverse(), axisLabel: { ...axisLabel, fontSize: 10 } },
                series: [
                  {
                    type: 'bar',
                    barMaxWidth: 16,
                    data: bench.data.statusDistribution
                      .map((x) => ({ value: x.count, name: x.status, itemStyle: { color: x.excluded ? '#b42318' : CHART_PALETTE[0] } }))
                      .reverse(),
                    label: { show: true, position: 'right' },
                  },
                ],
              }}
            />
          </ChartCard>
          <section className="card span-12" aria-label="Candidate Tracker rows">
            <div className="card-header">
              <h3>Candidate Tracker</h3>
              <span className="sub">Ordered by Bench Start Date (newest first)</span>
            </div>
            <div className="table-wrap scroll">
              <table className="data">
                <thead>
                  <tr>
                    <th>ShortName</th>
                    <th>Name</th>
                    <th>Grade</th>
                    <th>Location</th>
                    <th>Bench Status</th>
                    <th>Eligibility</th>
                    <th>Bench Start</th>
                    <th>Avail. Cap. (display only)</th>
                  </tr>
                </thead>
                <tbody>
                  {bench.data.rows.map((r) => (
                    <tr key={r.resourceId}>
                      <td className="mono">{r.shortName}</td>
                      <td>
                        <Link to={`/candidates/${r.candidateId}`}>{r.name ?? 'Not recorded'}</Link>
                      </td>
                      <td>{r.grade ?? 'Not recorded'}</td>
                      <td>{r.location}</td>
                      <td>{r.benchStatus}</td>
                      <td>
                        <EligibilityBadge eligibility={r.eligibility} reasons={r.exclusionReasons} />
                      </td>
                      <td>{formatDate(r.benchStartDate)}</td>
                      <td>{r.availableCapacity ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {show('match') && match.data && (
        <div className="grid-12" aria-label="Match and communication analytics">
          <ChartCard title="Match coverage" subtitle="Demands with ≥ 1 Strong/Potential match" table={{ columns: ['Coverage %'], rows: [[match.data.coveragePct]] }}>
            <EChart
              ariaLabel={`Match coverage ${match.data.coveragePct} percent`}
              option={{
                series: [
                  {
                    type: 'gauge',
                    startAngle: 90,
                    endAngle: -270,
                    pointer: { show: false },
                    progress: { show: true, roundCap: true, width: 14, itemStyle: { color: CHART_PALETTE[0] } },
                    axisLine: { lineStyle: { width: 14, color: [[1, '#eef1f5']] } },
                    splitLine: { show: false },
                    axisTick: { show: false },
                    axisLabel: { show: false },
                    detail: { valueAnimation: true, formatter: '{value} %', fontSize: 26, color: '#1d2939', offsetCenter: [0, 0] },
                    data: [{ value: match.data.coveragePct }],
                  },
                ],
              }}
            />
          </ChartCard>
          <ChartCard title="Band distribution" table={{ columns: ['Band', 'Rows'], rows: match.data.bandDistribution.map((x) => [bandLabel(x.label as Band), x.count]) }}>
            <EChart
              ariaLabel="Band distribution doughnut"
              onSelect={(b) => setFilters((f) => ({ ...f, band: b as Band }))}
              option={{
                tooltip: { trigger: 'item' },
                legend: { bottom: 0, textStyle: axisLabel },
                series: [
                  {
                    type: 'pie',
                    radius: ['45%', '70%'],
                    center: ['50%', '45%'],
                    label: { formatter: '{b}\n{d}%', fontSize: 11 },
                    data: match.data.bandDistribution
                      .filter((x) => x.count > 0)
                      .map((x) => ({ name: x.label, value: x.count, itemStyle: { color: BAND_HEX[x.label as Band] } })),
                  },
                ],
              }}
            />
          </ChartCard>
          <ChartCard title="Email status counts" table={{ columns: ['Status', 'Emails'], rows: match.data.emailStatusCounts.map((x) => [x.label, x.count]) }}>
            <EChart
              ariaLabel="Email status counts"
              option={{
                tooltip: { trigger: 'axis' },
                legend: { top: 0, textStyle: axisLabel },
                grid,
                xAxis: { type: 'value', axisLabel, minInterval: 1 },
                yAxis: { type: 'category', data: ['Emails'], axisLabel },
                series: match.data.emailStatusCounts.map((x, i) => ({
                  name: x.label,
                  type: 'bar',
                  stack: 'e',
                  data: [x.count],
                  barMaxWidth: 36,
                  itemStyle: { color: ['#667085', '#0b5cad', '#067647', '#b42318'][i] },
                  label: { show: x.count > 0 },
                })),
              }}
            />
          </ChartCard>
        </div>
      )}

      {show('quality') && match.data && (
        <section className="card" aria-label="Validation panel">
          <div className="card-header">
            <h3>Validation panel</h3>
            <span className="sub">
              {match.data.validation.filter((v) => v.passed).length}/{match.data.validation.length} checks passed
            </span>
          </div>
          {s && s.dataQualityIssues.total > 0 && (
            <Banner tone="warning">
              {s.dataQualityIssues.total} data-quality limitations (DL {s.dataQualityIssues.dl} · CT {s.dataQualityIssues.ct}). Review them in{' '}
              <Link to="/bench">Bench List</Link> and <Link to="/demands">Demand List</Link>.
            </Banner>
          )}
          <ul className="list-plain stack" style={{ gap: 6, marginTop: 8 }}>
            {match.data.validation.map((v) => (
              <li key={v.id} className="row">
                <span style={{ color: v.passed ? bandColorVar('Strong') : bandColorVar('Weak') }}>{v.passed ? '✓ Pass' : '✕ Fail'}</span>
                <span>{v.label}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
