import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'
import type { JobItem } from '../api/types'

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
