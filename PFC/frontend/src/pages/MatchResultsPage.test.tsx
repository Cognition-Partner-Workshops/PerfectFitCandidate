import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MatchRun } from '../api/types'
import { groupExcluded } from '../lib/excluded'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

describe('Match Results run selection (REQ-OUT-01)', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  it('shows the latest completed run while a newer run is in progress, then switches to it', async () => {
    const completed = (await mock.listMatchRuns())[0] as MatchRun
    const running: MatchRun = { ...completed, runId: 3, status: 'running', finishedAt: null }
    const done: MatchRun = { ...running, status: 'completed', finishedAt: '2026-11-06T09:00:00Z' }
    let runs = [running, completed]
    vi.spyOn(mock, 'listMatchRuns').mockImplementation(async () => runs)
    vi.spyOn(mock, 'getMatchRun')
      .mockResolvedValueOnce(running)
      .mockImplementation(async () => {
        runs = [done, completed]
        return done
      })
    const getResults = vi.spyOn(mock, 'getCurrentResults')

    renderApp('/matches')
    expect(await screen.findByText(/Scoring run 3 is running/)).toBeTruthy()
    expect(getResults).toHaveBeenCalledTimes(1)

    await waitFor(() => expect(getResults.mock.calls.length).toBeGreaterThan(1), { timeout: 4000 })
    await waitFor(() => expect(screen.queryByText(/Scoring run 3/)).toBeNull())
  })

  it('shows the run\'s excluded people with reasons in their own tab (REQ-ELIG-08)', async () => {
    const rows = groupExcluded(await mock.getCurrentExcluded(), await mock.listResources())
    expect(rows.length).toBeGreaterThan(0)
    renderApp('/matches')
    fireEvent.click(await screen.findByRole('tab', { name: `Excluded from matching (${rows.length})` }))
    const table = await screen.findByRole('table', { name: 'Excluded from matching' })
    expect(table.querySelectorAll('tbody tr').length).toBe(rows.length)
    expect(screen.getAllByText(rows[0].reasons[0].label).length).toBeGreaterThan(0)
    expect(screen.queryByRole('table', { name: 'Match results' })).toBeNull()
  })
})
