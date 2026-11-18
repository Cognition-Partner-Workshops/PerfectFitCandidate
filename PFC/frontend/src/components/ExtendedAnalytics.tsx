import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { EChartsCoreOption } from 'echarts/core'
import type { ExtendedAnalytics, MatrixCell } from '../api/reports'
import { emptyExtendedAnalytics } from '../api/reports'
import { formatDate, formatMetric, formatPct, formatServicePct } from '../lib/format'
import { EChart } from './EChart'
import { EmptyState, ErrorState, KpiTile, Spinner } from './ui'

type Section = 'demand' | 'bench' | 'match' | 'quality'
interface Card {
  id: number; title: string; section: Section; columns: string[]; rows: ReactNode[][]
  option?: EChartsCoreOption; note?: string; kpi?: number
}
const grid = { left: 12, right: 30, top: 38, bottom: 40, containLabel: true }

function bars(labels: string[], values: number[], horizontal = false): EChartsCoreOption {
  const category = { type: 'category', data: labels, axisLabel: { interval: 0, rotate: horizontal ? 0 : 25, fontSize: 11 } }
  return { animation: false, tooltip: { trigger: 'axis' }, grid, xAxis: horizontal ? { type: 'value' } : category, yAxis: horizontal ? category : { type: 'value' }, series: [{ type: 'bar', data: values, label: { show: true, position: horizontal ? 'right' : 'top' } }] }
}

function heatmap(cells: MatrixCell[], grades = false): EChartsCoreOption {
  const xs = [...new Set(cells.map((c) => c.column))]
  const ys = [...new Set(cells.map((c) => c.row))]
  return {
    animation: false, tooltip: {}, grid: { ...grid, bottom: 85 },
    xAxis: { type: 'category', data: xs, axisLabel: { rotate: 40, interval: 0 } },
    yAxis: { type: 'category', data: ys },
    visualMap: { min: 0, max: Math.max(1, ...cells.map((c) => c.count)), calculable: true, orient: 'horizontal', left: 'center', bottom: 0 },
    series: [{ type: 'heatmap', data: cells.map((c) => {
      const relation = 'relation' in c ? c.relation : undefined
      return { value: [xs.indexOf(c.column), ys.indexOf(c.row), c.count], label: { show: cells.length <= 100, fontSize: 9, formatter: grades ? relation === 'exact' ? '=' : relation === 'adjacent' ? '±1' : '' : String(c.count) }, itemStyle: { borderColor: '#fff', borderWidth: relation === 'exact' ? 3 : 1, borderType: relation === 'adjacent' ? 'dashed' : 'solid' } }
    }) }],
  }
}

function cards(d: ExtendedAnalytics, top5: boolean): Card[] {
  const count = (id: number, title: string, section: Section, rows: { label: string; count: number }[], horizontal = false): Card => ({ id, title, section, columns: ['Label', 'Count'], rows: rows.map((r) => [r.label, r.count]), option: bars(rows.map((r) => r.label), rows.map((r) => r.count), horizontal) })
  const matrix = (id: number, title: string, section: Section, rows: MatrixCell[]): Card => ({ id, title, section, columns: ['Row', 'Column', 'Count'], rows: rows.map((r) => [r.row, r.column, r.count]), option: heatmap(rows) })
  const bins = d.scoreHistogram.bins
  const histogram = bars(bins.map((b) => `${b.lower}–${b.upper}`), bins.map((b) => top5 ? b.top5 : b.allPairs))
  histogram.xAxis = { type: 'value', min: 0, max: 100, name: 'Match %', nameLocation: 'middle', nameGap: 30 }
  histogram.series = [{ type: 'bar', barWidth: '90%', data: bins.map((b) => [(b.lower + b.upper) / 2, top5 ? b.top5 : b.allPairs]), markLine: { symbol: 'none', data: d.scoreHistogram.thresholds.map((xAxis) => ({ xAxis, name: `${xAxis} %`, label: { formatter: `${xAxis} %` } })) } }]
  const groups = [...new Set(d.readinessStatus.map((r) => r.row))]
  const statuses = [...new Set(d.readinessStatus.map((r) => r.column))]
  const accuracy = d.accuracy.filter((r) => r.hitRateAt5 !== null || r.precisionAt5 !== null)
  return [
    { ...matrix(14, 'Demand grade × eligible bench grade', 'bench', d.gradeMatrix), columns: ['Demand grade', 'Bench grade', 'Pairs', 'Demand count', 'Bench count', 'Relation'], rows: d.gradeMatrix.map((r) => [r.row, r.column, r.count, r.demandCount, r.benchCount, r.relation === 'exact' ? '= exact' : r.relation === 'adjacent' ? '±1 adjacent' : 'Outside']), option: heatmap(d.gradeMatrix, true), note: '= exact grade; ±1 adjacent grade. These marks and borders distinguish eligibility without colour. Counts on hover or in the values table.' },
    { id: 16, title: 'Match-score histogram', section: 'match', columns: ['Lower', 'Upper', 'All pairs', 'Top-5'], rows: bins.map((b) => [b.lower, b.upper, b.allPairs, b.top5]), option: histogram },
    { ...count(17, 'Shortlist funnel', 'match', d.shortlistFunnel), option: { animation: false, tooltip: {}, series: [{ type: 'funnel', sort: 'none', left: '8%', width: '45%', label: { position: 'right', fontSize: 10, formatter: '{b}: {c}' }, data: d.shortlistFunnel.map((r) => ({ name: r.label, value: r.count })) }] } },
    { id: 18, title: 'Unmatched demands', section: 'demand', columns: ['Requisition', 'Start date', 'Reasons'], rows: [...d.unmatchedDemands].sort((a, b) => (a.positionStartDate ?? '9999').localeCompare(b.positionStartDate ?? '9999') || a.codeRequisition.localeCompare(b.codeRequisition)).map((r) => [r.codeRequisition, formatDate(r.positionStartDate), <span key={r.demandId}>{r.reasonCodes.map((code) => <span className="chip" key={code}>{code.replaceAll('_', ' ')}</span>)}</span>]), note: 'Start date ascending; missing dates last. No Strong, Potential or Avg match.' },
    { id: 19, title: 'Weekly time-to-email', section: 'match', columns: ['Week', 'Sent', 'Median hours', 'Within 24 h %'], rows: d.emailTrend.map((r) => [formatDate(r.week), r.sentCount, formatMetric(r.medianHours), formatServicePct(r.within24hPct)]), option: { animation: false, tooltip: { trigger: 'axis' }, legend: { data: ['Median hours', 'Within 24 h %'] }, grid, xAxis: { type: 'category', data: d.emailTrend.map((r) => formatDate(r.week)) }, yAxis: [{ type: 'value', name: 'Hours' }, { type: 'value', name: '%', min: 0, max: 100 }], series: [{ name: 'Median hours', type: 'line', data: d.emailTrend.map((r) => r.medianHours) }, { name: 'Within 24 h %', type: 'bar', yAxisIndex: 1, data: d.emailTrend.map((r) => r.within24hPct) }] } },
    { ...matrix(20, 'Readiness group × bench status', 'bench', d.readinessStatus), option: { animation: false, tooltip: { trigger: 'axis' }, legend: { type: 'scroll' }, grid, xAxis: { type: 'category', data: groups }, yAxis: { type: 'value' }, series: statuses.map((name) => ({ name, type: 'bar', stack: 'people', data: groups.map((group) => d.readinessStatus.find((r) => r.row === group && r.column === name)?.count ?? 0) })) } },
    matrix(21, 'Demand region × person location', 'demand', d.locationMatrix),
    matrix(22, 'Top-15 JD skill co-occurrence', 'demand', d.skillCooccurrence),
    { id: 24, title: 'Upskilling vs demanded skills', section: 'bench', columns: ['Name', 'Email (masked)', 'Matched skills', 'Missing skills', 'Best Match %'], rows: d.upskilling.map((r) => [r.name ?? 'Not recorded', r.emailMasked ?? 'Not recorded', r.matchedSkills.join('; ') || 'None', r.missingSkills.join('; ') || 'None', r.bestScore === null ? 'Not recorded' : formatPct(r.bestScore)]) },
    { id: 25, title: 'Auto-match accuracy by run', section: 'quality', columns: ['Run', 'Confirmed demands', 'Confirmed pairs', 'Hits', 'Hit rate at 5', 'Precision at 5'], rows: d.accuracy.map((r) => [r.runId, r.confirmedDemands, r.confirmedPairs, r.hits, formatMetric(r.hitRateAt5), formatMetric(r.precisionAt5)]), note: accuracy.length ? 'Confirmation proxy: active analyst pin/include or email approval. Ratios 0–1, not percentages.' : 'No analyst-confirmed shortlists yet', option: accuracy.length ? { animation: false, tooltip: { trigger: 'axis' }, legend: {}, grid, xAxis: { type: 'category', data: d.accuracy.map((r) => String(r.runId)) }, yAxis: { type: 'value', min: 0, max: 1 }, series: [{ name: 'Hit rate at 5', type: 'line', connectNulls: false, data: d.accuracy.map((r) => r.hitRateAt5) }, { name: 'Precision at 5', type: 'line', connectNulls: false, data: d.accuracy.map((r) => r.precisionAt5) }] } : undefined },
    count(26, 'Exclusion reasons', 'bench', d.exclusionReasons, true),
    { ...count(27, 'Candidate type split', 'bench', d.candidateTypes), option: { animation: false, tooltip: {}, legend: { bottom: 0 }, series: [{ type: 'pie', radius: ['45%', '70%'], data: d.candidateTypes.map((r) => ({ name: r.label, value: r.count })) }] } },
    { id: 28, title: 'Candidate contention', section: 'match', columns: ['Name', 'Email (masked)', 'Demands', 'Requisitions'], rows: [...d.contention].sort((a, b) => b.demandCount - a.demandCount || a.resourceId - b.resourceId).map((r) => [r.name ?? 'Not recorded', r.emailMasked ?? 'Not recorded', r.demandCount, r.requisitions.join('; ')]), option: bars([...d.contention].sort((a, b) => a.demandCount - b.demandCount || b.resourceId - a.resourceId).map((r) => r.name ?? String(r.resourceId)), [...d.contention].sort((a, b) => a.demandCount - b.demandCount || b.resourceId - a.resourceId).map((r) => r.demandCount), true) },
    { ...count(29, 'Demand aging', 'demand', d.demandAging.buckets), kpi: d.demandAging.pastStartWithoutMatch, note: `Unknown start: ${d.demandAging.unknownStart}` },
    { id: 30, title: 'Override rate per run', section: 'quality', columns: ['Run', 'Pairs', 'Overridden pairs', 'Rate %', 'By type'], rows: d.overrideRates.map((r) => [r.runId, r.pairCount, r.overriddenPairs, formatServicePct(r.ratePct), r.byType.map((t) => `${t.label}: ${t.count}`).join('; ') || 'None']), option: { ...bars(d.overrideRates.map((r) => String(r.runId)), d.overrideRates.map((r) => r.ratePct)), yAxis: { type: 'value', name: '%', min: 0, max: 100 } } },
  ]
}

function DeferredChart({ option, title }: { option: EChartsCoreOption; title: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    if (visible || !ref.current) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setVisible(true); observer.disconnect() }
    }, { rootMargin: '200px' })
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [visible])
  return <div ref={ref} style={{ minHeight: 320 }} data-chart-ready={visible}>{visible ? <EChart ariaLabel={title} option={option} height={320} /> : <p className="muted">Chart renders when in view.</p>}</div>
}

function AnalyticsCard({ card, children }: { card: Card; children?: ReactNode }) {
  const [table, setTable] = useState(false)
  const [limit, setLimit] = useState(50)
  return <section className={`card span-${card.option ? 6 : 12}`} aria-label={`DB-${card.id} ${card.title}`} data-extended-card={card.id} data-data-state="ready">
    <div className="card-header"><h3>DB-{card.id} {card.title}</h3><span className="spacer" />{card.option && <button className="btn btn-tertiary btn-sm" aria-pressed={table} onClick={() => setTable(!table)}>View as {table ? 'chart' : 'table'}</button>}</div>
    {card.note && <p className="small muted">{card.note}</p>}{card.kpi !== undefined && <KpiTile label="Past start without match" value={card.kpi} />}{children}
    {!card.rows.length ? <EmptyState message="No data for the current filters" /> : card.option && !table ? <DeferredChart option={card.option} title={`DB-${card.id} ${card.title}`} /> : <><div className="table-wrap scroll"><table className="data"><thead><tr>{card.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead><tbody>{card.rows.slice(0, limit).map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>{limit < card.rows.length && <button className="btn" onClick={() => setLimit(limit + 50)}>Show more ({limit} of {card.rows.length})</button>}</>}
  </section>
}

export function ExtendedAnalyticsSection({ section, data, pending, error, onRetry }: { section: Section; data?: ExtendedAnalytics; pending: boolean; error: unknown; onRetry: () => void }) {
  const [top5, setTop5] = useState(false)
  const items = useMemo(() => cards(data ?? emptyExtendedAnalytics(), top5).filter((c) => c.section === section), [data, top5, section])
  if (error || pending) return <div className="grid-12" aria-label={`Extended ${section} analytics`}>{items.map((card) => <section key={card.id} className="card span-6" aria-label={`DB-${card.id} ${card.title}`}><h3>DB-{card.id} {card.title}</h3>{error ? <ErrorState error={error} onRetry={onRetry} /> : <Spinner />}</section>)}</div>
  return <div className="grid-12" aria-label={`Extended ${section} analytics`}>{items.map((card) => <AnalyticsCard key={card.id} card={card}>{card.id === 16 && <label className="field">Histogram population<select value={top5 ? 'top5' : 'all'} onChange={(e) => setTop5(e.target.value === 'top5')}><option value="all">All pairs</option><option value="top5">Top-5</option></select></label>}</AnalyticsCard>)}</div>
}
