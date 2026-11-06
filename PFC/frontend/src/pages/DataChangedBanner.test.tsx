import { screen } from '@testing-library/react'
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
    vi.spyOn(mock, 'getMatchRun').mockResolvedValue(run)
    renderApp(path)
    expect(await screen.findByText('Data changed since this run', {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(/DL\/CT committed, CV synced/)).toBeTruthy()
    expect(screen.getByText(/point-in-time snapshot/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Re-run matching' }).getAttribute('href')).toContain('/demands')
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
