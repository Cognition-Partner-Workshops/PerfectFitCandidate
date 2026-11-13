import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MatchRun } from '../api/types'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

describe('Data changed since this run banner', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  const stale = async (): Promise<MatchRun> => ({
    ...((await mock.listMatchRuns())[0] as MatchRun),
    dataChangedSinceRun: true,
    dataChangedReasons: ['DL/CT committed', 'CV synced'],
  })

  it.each(['/matches', '/'])('names the reasons and links to Confirm & Start on %s', async (path) => {
    const run = await stale()
    vi.spyOn(mock, 'listMatchRuns').mockResolvedValue([run])
    const current = await mock.getCurrentResultsSummary()
    vi.spyOn(mock, 'getCurrentResultsSummary').mockResolvedValue({
      ...current, runs: [run], dataChangedSinceRun: true, dataChangedReasons: run.dataChangedReasons,
      staleDemandIds: run.demandIds,
    })
    vi.spyOn(mock, 'getMatchRun').mockResolvedValue(run)
    renderApp(path)
    expect(await screen.findByText('Data changed since this run', {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(/DL\/CT committed, CV synced/)).toBeTruthy()
    expect(screen.getByText(/point-in-time snapshot/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Full re-run' }).getAttribute('href')).toContain('/demands')
  })

  it('Re-run changed only starts an incremental run for the stale demand IDs from the current summary', async () => {
    const run = { ...(await stale()), scope: 'demands', demandIds: [3, 7] }
    const current = await mock.getCurrentResultsSummary()
    vi.spyOn(mock, 'getCurrentResultsSummary').mockResolvedValue({ ...current, runs: [run], dataChangedSinceRun: true, dataChangedReasons: run.dataChangedReasons, staleDemandIds: [3, 7] })
    vi.spyOn(mock, 'listMatchRuns').mockResolvedValue([run])
    vi.spyOn(mock, 'getMatchRun').mockResolvedValue(run)
    const start = vi.spyOn(mock, 'startMatchRun')
    renderApp('/matches')
    fireEvent.click(await screen.findByRole('button', { name: 'Re-run changed only' }, { timeout: 5000 }))
    await waitFor(() => expect(start).toHaveBeenCalledWith({ incremental: true, scope: 'demands', demandIds: [3, 7] }))
  })

  it('mock clears the banner after an incremental re-run and records the mode', async () => {
    await mock.startMatchRun({ incremental: true, scope: 'full', demandIds: [] })
    expect((await mock.listMatchRuns())[0]).toMatchObject({ dataChangedSinceRun: false, mode: 'incremental', modeReason: 'inputs_compared' })
    await mock.startMatchRun({ incremental: true, scope: 'demands', demandIds: [1] })
    expect((await mock.listMatchRuns())[0]).toMatchObject({ mode: 'full', modeReason: 'scope_not_full' })
  })

  it('shows the run mode and a full run fallback reason in the run summary', async () => {
    const run = { ...(await stale()), mode: 'full' as const, modeReason: 'config_changed' }
    const current = await mock.getCurrentResultsSummary()
    vi.spyOn(mock, 'getCurrentResultsSummary').mockResolvedValue({ ...current, runs: [run] })
    vi.spyOn(mock, 'listMatchRuns').mockResolvedValue([run])
    vi.spyOn(mock, 'getMatchRun').mockResolvedValue(run)
    renderApp('/matches')
    expect(await screen.findByText(/Mode: full \(config_changed\)/, {}, { timeout: 5000 })).toBeTruthy()
  })

  it('Dashboard shows the mode of the run it loads by id and refreshes the summary after a re-run', async () => {
    const run = { ...(await stale()), mode: 'full' as const, modeReason: 'scope_not_full' }
    vi.spyOn(mock, 'getMatchRun').mockResolvedValue(run)
    const summary = vi.spyOn(mock, 'analyticsSummary')
    renderApp('/')
    expect(await screen.findByText(/Mode: full \(scope_not_full\)/, {}, { timeout: 5000 })).toBeTruthy()
    const calls = summary.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Re-run changed only' }))
    await waitFor(() => expect(summary.mock.calls.length).toBeGreaterThan(calls), { timeout: 5000 })
  })

  it('Dashboard looks up the summary run by id, not in the capped run list', async () => {
    const run = await stale()
    vi.spyOn(mock, 'listMatchRuns').mockResolvedValue([])
    const get = vi.spyOn(mock, 'getMatchRun').mockResolvedValue(run)
    renderApp('/')
    expect(await screen.findByText('Data changed since this run', {}, { timeout: 5000 })).toBeTruthy()
    expect(get).toHaveBeenCalledWith(run.runId)
  })

  it('is hidden when nothing changed after the run', async () => {
    renderApp('/matches')
    expect(await screen.findByRole('heading', { name: 'Match Results' })).toBeTruthy()
    expect(screen.queryByText('Data changed since this run')).toBeNull()
  })

  it('mock flags DL/CT commits, CV syncs and JD confirmations against the completed run', async () => {
    await mock.commitIngestion(1)
    await mock.syncCvs([(await mock.listResources())[0].resourceId])
    expect((await mock.listMatchRuns())[0]).toMatchObject({ dataChangedSinceRun: true, dataChangedReasons: ['DL/CT committed', 'CV synced'] })
    await mock.confirmRequirements((await mock.listDemands())[0].demandId)
    expect((await mock.listMatchRuns())[0]).toMatchObject({ dataChangedSinceRun: true, dataChangedReasons: ['DL/CT committed', 'CV synced', 'JD confirmed'] })
  })
})
