import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { emptyExtendedAnalytics } from '../api/reports'
import { ExtendedAnalyticsSection } from './ExtendedAnalytics'

const chart = vi.hoisted(() => vi.fn())
vi.mock('./EChart', () => ({ EChart: (props: { ariaLabel: string; option: unknown }) => { chart(props); return <div role="img" aria-label={props.ariaLabel} /> } }))

function fixture() {
  const d = emptyExtendedAnalytics()
  d.gradeMatrix = [{ row: 'C1', column: 'C1', count: 4, demandCount: 2, benchCount: 2, relation: 'exact' }, { row: 'C1', column: 'C2', count: 2, demandCount: 2, benchCount: 1, relation: 'adjacent' }]
  d.scoreHistogram.bins = [{ lower: 90, upper: 95, allPairs: 12, top5: 5 }]
  d.shortlistFunnel = [{ label: 'Matched', count: 2 }]
  d.unmatchedDemands = [{ demandId: 1, codeRequisition: 'LATER', positionStartDate: '2026-06-01', reasonCodes: ['mandatory_skill_gap'] }, { demandId: 2, codeRequisition: 'EARLIER', positionStartDate: '2026-01-01', reasonCodes: ['insufficient_data'] }]
  d.emailTrend = [{ week: '2026-01-05', sentCount: 3, medianHours: 12.25, within24hPct: 66.67 }]
  d.readinessStatus = [{ row: 'Ready', column: 'Available', count: 3 }]
  d.locationMatrix = [{ row: 'North', column: 'Manchester', count: 2 }]
  d.skillCooccurrence = [{ row: 'SQL', column: 'Python', count: 3 }]
  const person = { resourceId: 1, candidateId: 1, name: 'Taylor', emailMasked: 'ta****@example.com' }
  d.upskilling = [{ ...person, matchedSkills: ['SQL'], missingSkills: ['Python'], bestScore: 90.5 }]
  d.accuracy = [{ runId: 1, confirmedDemands: 2, confirmedPairs: 2, hits: 1, hitRateAt5: 0.5, precisionAt5: 0.1 }]
  d.exclusionReasons = [{ label: 'Status excluded', count: 2 }]
  d.candidateTypes = [{ label: 'Internal', count: 3 }]
  d.contention = [{ ...person, demandCount: 3, requisitions: ['R1', 'R2', 'R3'] }]
  d.demandAging = { buckets: [{ label: '0–7 days', count: 3 }], unknownStart: 1, pastStartWithoutMatch: 2 }
  d.overrideRates = [{ runId: 1, pairCount: 10, overriddenPairs: 1, ratePct: 10, byType: [{ label: 'pin', count: 1 }] }]
  return d
}

describe('extended dashboard cards', () => {
  it('renders all 15 datasets with accessible tables and masked people', () => {
    for (const section of ['demand', 'bench', 'match', 'quality'] as const) render(<ExtendedAnalyticsSection section={section} data={fixture()} pending={false} error={null} onRetry={vi.fn()} />)
    expect(document.querySelectorAll('[data-extended-card]').length).toBe(15)
    expect(screen.getByText('91 %')).toBeTruthy()
    expect(screen.getByText('ta****@example.com')).toBeTruthy()
    const rows = within(screen.getByRole('region', { name: /DB-18/ })).getAllByRole('row')
    expect(rows[1]?.textContent).toContain('EARLIER')
    expect(rows[2]?.textContent).toContain('mandatory skill gap')
    fireEvent.click(within(screen.getByRole('region', { name: /DB-14/ })).getByRole('button'))
    expect(screen.getByText('= exact')).toBeTruthy()
    expect(screen.getByText('±1 adjacent')).toBeTruthy()
    expect(screen.getByText('Past start without match')).toBeTruthy()
    const heatmap = chart.mock.calls.map(([props]) => props).filter((p) => p.ariaLabel.startsWith('DB-14')).at(-1)!.option
    expect(heatmap.series[0].data.map((r: { label: { formatter: string } }) => r.label.formatter)).toEqual(['=', '±1'])
  })

  it('switches the histogram population and retains threshold mark lines', () => {
    render(<ExtendedAnalyticsSection section="match" data={fixture()} pending={false} error={null} onRetry={vi.fn()} />)
    const histogram = () => chart.mock.calls.map(([props]) => props).filter((p) => p.ariaLabel.startsWith('DB-16')).at(-1)!.option
    expect(histogram().series[0].data).toEqual([[92.5, 12]])
    fireEvent.change(screen.getByLabelText('Histogram population'), { target: { value: 'top5' } })
    expect(histogram().series[0].data).toEqual([[92.5, 5]])
    expect(histogram().series[0].markLine.data.map((r: { xAxis: number }) => r.xAxis)).toEqual([61, 70, 90])
  })

  it('handles empty data and absent confirmation without inventing accuracy', () => {
    render(<ExtendedAnalyticsSection section="quality" data={emptyExtendedAnalytics()} pending={false} error={null} onRetry={vi.fn()} />)
    expect(screen.getByText('No analyst-confirmed shortlists yet')).toBeTruthy()
    expect(screen.getAllByText('No data for the current filters').length).toBe(2)
    expect(screen.queryByRole('img')).toBeNull()
  })

  it('leaves a gap in accuracy lines for runs without confirmations', () => {
    const data = fixture()
    data.accuracy.push({ runId: 2, confirmedDemands: 0, confirmedPairs: 0, hits: 0, hitRateAt5: null, precisionAt5: null })
    render(<ExtendedAnalyticsSection section="quality" data={data} pending={false} error={null} onRetry={vi.fn()} />)
    const option = chart.mock.calls.map(([props]) => props).filter((p) => p.ariaLabel.startsWith('DB-25')).at(-1)!.option
    expect(option.xAxis.data).toEqual(['1', '2'])
    expect(option.series[0].data).toEqual([0.5, null])
    expect(option.series[0].connectNulls).toBe(false)
  })

  it('shows loading and retry errors on individual cards', () => {
    const retry = vi.fn()
    const { rerender } = render(<ExtendedAnalyticsSection section="quality" pending error={null} onRetry={retry} />)
    expect(screen.getByRole('region', { name: /DB-25/ })).toBeTruthy()
    rerender(<ExtendedAnalyticsSection section="quality" pending={false} error={new Error('Unavailable')} onRetry={retry} />)
    fireEvent.click(screen.getAllByRole('button', { name: /Retry/ })[0]!)
    expect(retry).toHaveBeenCalledOnce()
  })
})
