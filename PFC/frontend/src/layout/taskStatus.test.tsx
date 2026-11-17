import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'
import type { JobItem } from '../api/types'
import { formatDateTime } from '../lib/format'

beforeEach(async () => { await seed(); await signIn('Admin', ADMIN.email, ADMIN.password) })
afterEach(() => vi.restoreAllMocks())

it('shows truthful busy status and selected local model, preserves cards across navigation', async () => {
  renderApp('/bench')
  expect(await screen.findByRole('status', { name: 'Sync: tasks in progress' })).toBeTruthy()
  const model = (await mock.listModels()).find((item) => item.isDefault && item.purpose === 'generation')!
  expect(await screen.findByText(new RegExp(`Local model: ${model.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`))).toBeTruthy()
  fireEvent.click(screen.getByRole('link', { name: 'Demand List' }))
  expect(await screen.findByRole('heading', { name: 'Demand List' })).toBeTruthy()
  expect(screen.getByText('CV Sync for 3 profiles: In Progress (45%)')).toBeTruthy()
})

it('shows recent failures and model fallback even after the failed card is dismissed', async () => {
  const failed: JobItem = { jobId: 99, kind: 'embed', status: 'failed', progress: 0, message: 'private exception', label: 'Model check', link: null, createdAt: '', startedAt: null, finishedAt: '' }
  vi.spyOn(mock, 'listJobs').mockResolvedValue([failed])
  vi.spyOn(mock, 'listModels').mockResolvedValue([])
  renderApp('/bench')
  expect(await screen.findByRole('status', { name: 'Sync: a task failed' })).toBeTruthy()
  expect(await screen.findByText(/Local model: not loaded/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss Model check' }))
  expect(screen.queryByRole('button', { name: 'Dismiss Model check' })).toBeNull()
  expect(screen.getByRole('status', { name: 'Sync: a task failed' })).toBeTruthy()
})

it('shows idle when no tasks are active or recently failed', async () => {
  vi.spyOn(mock, 'listJobs').mockResolvedValue([])
  renderApp('/bench')
  expect(await screen.findByRole('status', { name: 'Sync: no tasks in progress' })).toBeTruthy()
})

it('loads a linked historical run and its rows instead of current rows', async () => {
  const loader = vi.spyOn(mock, 'getMatchRun')
  const rows = vi.spyOn(mock, 'getResults')
  renderApp('/matches?runId=2')
  await waitFor(() => expect(loader).toHaveBeenCalledWith(2))
  await waitFor(() => expect(rows).toHaveBeenCalledWith(2))
  expect(await screen.findByText(/Run #2 · completed/)).toBeTruthy()
  await act(async () => { fireEvent.click(screen.getByRole('link', { name: 'Show current results' })) })
  expect(await screen.findByText(/Current results ·/)).toBeTruthy()
})

it('refreshes historical result rows after saving an override', async () => {
  const rows = vi.spyOn(mock, 'getResults')
  renderApp('/matches?runId=2')
  const button = (await screen.findAllByRole('button', { name: 'Override' }))[0]
  const row = button.closest('tr')!
  expect(within(row).queryByText('Overridden')).toBeNull()
  fireEvent.click(button)
  fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Reviewed historical result' } })
  const calls = rows.mock.calls.length
  fireEvent.click(screen.getByRole('button', { name: 'Save override' }))
  await waitFor(() => expect(rows.mock.calls.length).toBeGreaterThan(calls))
  expect(await within(row).findByText('Overridden')).toBeTruthy()
})

it('shows bulk analysis partial failures without breaking the successful response refresh', async () => {
  const failed = vi.spyOn(mock, 'analyzeDemands').mockResolvedValue({ analyzed: [], failed: [{ demandId: 1, detail: 'private@example.com /home/private/file' }] })
  const demands = vi.spyOn(mock, 'listDemands')
  renderApp('/demands')
  const button = await screen.findByRole('button', { name: /Analyze all new/ })
  const calls = demands.mock.calls.length
  fireEvent.click(button)
  await waitFor(() => expect(failed).toHaveBeenCalled())
  const count = failed.mock.calls[0][0].length
  expect(await screen.findByText(`Analyze all new (${count}): Failed — 1 of ${count} failed`)).toBeTruthy()
  await waitFor(() => expect(demands.mock.calls.length).toBeGreaterThan(calls))
})

it('uses the selected historical run date in every requisition heading', async () => {
  const run = await mock.getMatchRun(1)
  vi.spyOn(mock, 'getMatchRun').mockResolvedValue({ ...run, finishedAt: '2020-01-02T12:00:00Z' })
  renderApp('/matches?runId=1')
  const headings = await screen.findAllByRole('button', { name: /^Toggle / })
  for (const heading of headings) {
    expect(heading.closest('tr')?.textContent).toContain(`Scored ${formatDateTime('2020-01-02T12:00:00Z')} · Run 1`)
  }
})
