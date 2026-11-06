import { screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MatchRun } from '../api/types'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

describe('Match Results run selection (REQ-OUT-01)', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  it('shows the latest completed run while a newer run is in progress, then switches to it', async () => {
    const completed = (await mock.listMatchRuns())[0] as MatchRun
    const running: MatchRun = { ...completed, runId: 2, status: 'running', finishedAt: null }
    const done: MatchRun = { ...running, status: 'completed', finishedAt: '2026-11-06T09:00:00Z' }
    let runs = [running, completed]
    vi.spyOn(mock, 'listMatchRuns').mockImplementation(async () => runs)
    vi.spyOn(mock, 'getMatchRun')
      .mockResolvedValueOnce(running)
      .mockImplementation(async () => {
        runs = [done, completed]
        return done
      })
    const results = mock.getResults.bind(mock)
    const getResults = vi.spyOn(mock, 'getResults').mockImplementation(() => results(1))

    renderApp('/matches')
    expect(await screen.findByText(/Scoring run 2 is running/)).toBeTruthy()
    expect(getResults).toHaveBeenCalledWith(1)
    expect(getResults).not.toHaveBeenCalledWith(2)

    await waitFor(() => expect(getResults).toHaveBeenCalledWith(2), { timeout: 4000 })
    expect(screen.queryByText(/Scoring run 2/)).toBeNull()
  })
})
