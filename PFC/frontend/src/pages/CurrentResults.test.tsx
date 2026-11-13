import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'
import type { MatchResult } from '../api/types'

describe('combined results and shared filters', () => {
  let rows: MatchResult[]
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
    rows = await mock.getCurrentResults()
  })
  afterEach(() => vi.restoreAllMocks())

  async function loaded(path = '/matches') {
    renderApp(path)
    await screen.findByRole('heading', { name: 'Match Results' })
  }
  function groups() {
    return screen.queryAllByRole('button', { name: /^Toggle / }).map((button) => button.getAttribute('aria-label')?.replace('Toggle ', ''))
  }
  function checkFilter(label: string, values: string[]) {
    fireEvent.click(screen.getByRole('button', { name: label }))
    const popover = screen.getByRole('group', { name: `${label} options` })
    for (const value of values) fireEvent.click(within(popover).getByRole('checkbox', { name: value }))
    fireEvent.keyDown(popover, { key: 'Escape' })
  }
  function visibleIds() {
    return [...document.querySelectorAll('table[aria-label="Match results"] tbody input[type="checkbox"]')]
      .map((input) => input.getAttribute('aria-label'))
  }

  it('shows rows from two runs and at least three requisitions with scoring metadata', async () => {
    await loaded()
    expect(new Set(rows.map((r) => r.runId)).size).toBe(2)
    expect(groups().length).toBeGreaterThanOrEqual(3)
    expect(visibleIds().length).toBe(rows.length)
    expect(screen.getByText(/Current results · .* reqs · 2 runs · latest/)).toBeTruthy()
    expect(screen.getAllByText(/Scored .* · Run /).length).toBe(groups().length)
  })

  it('defaults to All, selects two requisitions, then resets to All', async () => {
    await loaded()
    const codes = [...new Set(rows.map((r) => r.codeRequisition))].slice(0, 2)
    expect(screen.getByRole('button', { name: 'Code Requisition' }).textContent).toBe('All')
    checkFilter('Code Requisition', codes)
    expect(groups().sort()).toEqual(codes.sort())
    expect(screen.getByRole('button', { name: 'Code Requisition' }).textContent).toBe('2 selected')
    checkFilter('Code Requisition', ['All'])
    expect(groups().length).toBe(new Set(rows.map((r) => r.codeRequisition)).size)
    expect(screen.getByRole('button', { name: 'Code Requisition' }).textContent).toBe('All')
  })

  it('accepts two Band values with OR semantics', async () => {
    await loaded()
    checkFilter('Band', ['Strong', 'Potential'])
    expect(screen.getByRole('button', { name: 'Band' }).textContent).toBe('2 selected')
    expect(visibleIds().length).toBe(rows.filter((r) => ['Strong', 'Potential'].includes(r.band)).length)
  })

  it('collapses individual and all groups, retaining headers and selections', async () => {
    await loaded()
    const first = screen.getAllByRole('button', { name: /^Toggle / })[0]
    const before = visibleIds().length
    const selectedBefore = document.querySelectorAll('tbody input:checked').length
    fireEvent.click(first)
    expect(first.getAttribute('aria-expanded')).toBe('false')
    expect(visibleIds().length).toBeLessThan(before)
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }))
    expect(visibleIds()).toEqual([])
    expect(groups().length).toBe(new Set(rows.map((r) => r.codeRequisition)).size)
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }))
    expect(visibleIds().length).toBe(before)
    expect(document.querySelectorAll('tbody input:checked').length).toBe(selectedBefore)
  })

  it('orders Flat list by score descending, with requisition and rank ties', async () => {
    await loaded()
    fireEvent.click(screen.getByRole('radio', { name: 'Flat list' }))
    expect(groups()).toEqual([])
    const expected = [...rows].sort((a, b) => b.score - a.score || a.codeRequisition.localeCompare(b.codeRequisition) || a.rank - b.rank)
    const labels = expected.map((r) => `Select ${r.personName ?? 'candidate'} for ${r.codeRequisition}`)
    expect(visibleIds()).toEqual(labels)
  })

  it('shares filters on Dashboard navigation and persists them in sessionStorage', async () => {
    const analytics = vi.spyOn(mock, 'analyticsSummary')
    await loaded()
    const codes = [...new Set(rows.map((r) => r.codeRequisition))].slice(0, 2)
    checkFilter('Code Requisition', codes)
    checkFilter('Band', ['Strong', 'Potential'])
    fireEvent.click(screen.getByRole('link', { name: 'Dashboard' }))
    await screen.findByRole('heading', { name: 'Dashboard' }, { timeout: 5000 })
    await waitFor(() => expect(analytics).toHaveBeenCalledWith(expect.objectContaining({ codeRequisition: codes, band: ['Strong', 'Potential'] })), { timeout: 5000 })
    expect(screen.getByRole('button', { name: 'Code Requisition' }).textContent).toBe('2 selected')
    expect(screen.getByRole('button', { name: 'Band' }).textContent).toBe('2 selected')
    expect(JSON.parse(sessionStorage.getItem('pfc-result-filters') ?? '{}').codeRequisition).toEqual(codes)
  })

  it('seeds only requisition from the URL, retaining other shared filters', async () => {
    sessionStorage.setItem('pfc-result-filters', JSON.stringify({ band: ['Strong'], codeRequisition: ['old'] }))
    const code = rows[0].codeRequisition
    await loaded(`/matches?req=${code}`)
    expect(groups()).toEqual([code])
    expect(screen.getByRole('button', { name: 'Band' }).textContent).toBe('Strong')
  })

  it('shows an empty state when current results are empty', async () => {
    vi.spyOn(mock, 'getCurrentResults').mockResolvedValue([])
    await loaded()
    expect(screen.getByText('No results match the current filters.')).toBeTruthy()
  })
  it('reports a current-results loading error', async () => {
    vi.spyOn(mock, 'getCurrentResults').mockRejectedValue(new Error('Current results unavailable'))
    await loaded()
    expect(await screen.findByText('Current results unavailable')).toBeTruthy()
  })

  it('sends selected IDs without a run ID', async () => {
    const send = vi.spyOn(mock, 'createDrafts').mockResolvedValue([])
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: /Send selected to Email Approval/ }))
    await waitFor(() => expect(send).toHaveBeenCalled())
    expect(send.mock.calls[0].length).toBe(1)
    expect(send.mock.calls[0][0].length).toBeGreaterThan(0)
  })
})
