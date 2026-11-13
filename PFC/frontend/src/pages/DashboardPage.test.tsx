import { screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MatchResult } from '../api/types'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

describe('Dashboard current-result summaries', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  it('filters match rows before choosing a demand best band', async () => {
    const results = await mock.getCurrentResults()
    const target = results[0]!
    const demandAnalytics = await mock.analyticsDemand({})
    const targetDemand = demandAnalytics.rows.find((demand) => demand.demandId === target.demandId)
    expect(targetDemand).toBeDefined()
    const rows: MatchResult[] = [
      {
        ...target,
        resultId: 90001,
        band: 'Strong',
        effectiveBand: 'Strong',
        personLocation: 'London',
        effectiveExcluded: false,
      },
      {
        ...target,
        resultId: 90002,
        band: 'Potential',
        effectiveBand: 'Potential',
        personLocation: 'Glasgow',
        effectiveExcluded: false,
      },
    ]
    sessionStorage.setItem('pfc-result-filters', JSON.stringify({ location: ['Glasgow'] }))
    vi.spyOn(mock, 'getCurrentResults').mockResolvedValue(rows)
    vi.spyOn(mock, 'analyticsDemand').mockResolvedValue({
      ...demandAnalytics,
      rows: [targetDemand!],
    })
    renderApp('/dashboard')

    const region = await screen.findByRole('region', { name: 'Demand List rows' })
    const table = within(region).getByRole('table')
    const row = within(table).getByText(targetDemand!.codeRequisition).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row!).getByText('Potential')).toBeTruthy()
    expect(within(row!).queryByText('Strong')).toBeNull()
  })

  it('ignores the saved person-grade filter when choosing a demand best band', async () => {
    const results = await mock.getCurrentResults()
    const target = results[0]!
    const demandAnalytics = await mock.analyticsDemand({})
    const targetDemand = demandAnalytics.rows.find((demand) => demand.demandId === target.demandId)
    expect(targetDemand).toBeDefined()
    const rows: MatchResult[] = [
      {
        ...target,
        resultId: 90003,
        band: 'Strong',
        effectiveBand: 'Strong',
        personGrade: 'D1',
        effectiveExcluded: false,
      },
      {
        ...target,
        resultId: 90004,
        band: 'Potential',
        effectiveBand: 'Potential',
        personGrade: 'C1',
        effectiveExcluded: false,
      },
    ]
    sessionStorage.setItem('pfc-result-filters', JSON.stringify({ personGrade: ['C1'] }))
    vi.spyOn(mock, 'getCurrentResults').mockResolvedValue(rows)
    vi.spyOn(mock, 'analyticsDemand').mockResolvedValue({
      ...demandAnalytics,
      rows: [targetDemand!],
    })
    renderApp('/dashboard')

    const region = await screen.findByRole('region', { name: 'Demand List rows' })
    const table = within(region).getByRole('table')
    const row = within(table).getByText(targetDemand!.codeRequisition).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row!).getByText('Strong')).toBeTruthy()
    expect(within(row!).queryByText('Potential')).toBeNull()
  })

  it('shows aggregate staleness when an older contributing run is stale and newest is fresh', async () => {
    const summary = await mock.getCurrentResultsSummary()
    const newest = await mock.getMatchRun(2)
    const older = await mock.getMatchRun(1)
    const currentSummary = {
      ...summary,
      runs: summary.runs.map((run) => ({
        ...run,
        dataChangedSinceRun: run.runId === older.runId,
        dataChangedReasons: run.runId === older.runId ? ['JD confirmed'] : [],
      })),
      dataChangedSinceRun: true,
      dataChangedReasons: ['JD confirmed'],
      staleDemandIds: summary.demands
        .filter((demand) => demand.runId === older.runId)
        .map((demand) => demand.demandId),
    }
    const getMatchRun = vi.spyOn(mock, 'getMatchRun').mockImplementation(async (runId) =>
      runId === newest.runId
        ? { ...newest, dataChangedSinceRun: false, dataChangedReasons: [] }
        : { ...older, dataChangedSinceRun: true, dataChangedReasons: ['JD confirmed'] },
    )
    vi.spyOn(mock, 'getCurrentResultsSummary').mockResolvedValue(currentSummary)
    renderApp('/dashboard')

    expect(await screen.findByText('Data changed since this run')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Re-run changed only' })).toBeTruthy()
    expect(getMatchRun).toHaveBeenCalledWith(newest.runId)
  })
})
