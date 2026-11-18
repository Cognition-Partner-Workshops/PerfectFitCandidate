import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { api } from '../api'
import type { ArchiveBatch, LifecycleDetail } from '../api/maintenance'
import { EndedRecords, LifecycleControls, LifecycleDialog, LifecycleHistory, LifecycleMenu } from '../components/Lifecycle'
import { archiveReasonLabel, countLabel } from '../lib/lifecycle'
import { ArchivePage } from './ArchivePage'
import { OccArchival } from './occ/OccArchival'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

const detail: LifecycleDetail = { lifecycle: 'open', updated_at: '2026-11-18', end_source: null, end_outcome: null, lifecycle_events: [{ event_id: 4, from_state: null, to_state: 'open', source: 'upload', outcome: null, reason_code: null, comment: null, created_at: '2026-11-18' }] }
const batch: ArchiveBatch = { batch_id: 2, kind: 'archive', status: 'previewed', preview_hash: 'hash1', preview: { demand: 1, resource: 1 }, result: null, items: [
  { entity_type: 'demand', entity_id: 1, label: 'T10001', reason: 'filled_manual', included: 1, outcome: null },
  { entity_type: 'resource', entity_id: 2, label: 'Resource 2', reason: 'ended', included: 1, outcome: null },
] }
const show = (node: ReactNode) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>)
beforeEach(() => {
  vi.spyOn(api.maintenance, 'reasons').mockResolvedValue({ filled: ['position_filled', 'other'], reopen: ['correction', 'other'] })
  vi.spyOn(api.maintenance, 'settings').mockResolvedValue({ 'archive.retention_months': 12 })
  vi.spyOn(api.maintenance, 'cleanupPreview').mockResolvedValue({ temp: { count: 2, bytes: 1024 }, logs: { count: 1, bytes: 1024 }, orphan_chunks: 0, orphan_vectors: 1, db_bytes: 1024, backups: [] })
})
afterEach(() => vi.restoreAllMocks())

it.each([
  ['filled_manual', 'Filled — manual'], ['cancelled_manual', 'Cancelled — manual'],
  ['not_in_latest_dl', 'Not in latest DL'], ['not_in_latest_ct', 'Off bench — not in latest CT'],
  ['selected', 'Selected'], ['not_on_bench', 'Not on Bench'], ['bench_end_set', 'Bench end date set'],
  ['closed_legacy', 'Closed — legacy record'], ['retention_expired', 'Retention expired'],
])('renders archive reason %s accurately', (code, label) => {
  expect(archiveReasonLabel(code)).toBe(label)
})

it.each([0, 1, 2])('pluralizes counts of %s consistently', (count) => {
  for (const noun of ['record', 'demand', 'resource', 'day', 'calendar month', 'housekeeping backup']) {
    expect(countLabel(count, noun)).toBe(`${count} ${noun}${count === 1 ? '' : 's'}`)
  }
})

it('offers one compact row control and loads lifecycle actions on demand', async () => {
  const load = vi.spyOn(api.maintenance, 'detail').mockResolvedValue(detail)
  show(<LifecycleMenu entity="demand" id={1} />)
  expect(screen.getAllByRole('button')).toHaveLength(1)
  expect(load).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Lifecycle' }))
  const menu = screen.getByRole('dialog', { name: 'Lifecycle actions' })
  expect(await within(menu).findByRole('button', { name: 'End of cycle…' })).toBeTruthy()
  fireEvent.click(within(menu).getByRole('button', { name: 'Lifecycle history' }))
  expect(within(menu).getByRole('region', { name: 'Lifecycle history' })).toBeTruthy()
})

it('requires other comment and submits lifecycle concurrency tokens', async () => {
  const change = vi.spyOn(api.maintenance, 'change').mockResolvedValue({})
  const close = vi.fn()
  show(<LifecycleDialog entity="demand" id={1} detail={detail} reopen={false} onClose={close} />)
  await screen.findByRole('option', { name: 'Other' })
  fireEvent.change(await screen.findByLabelText('Reason (required)'), { target: { value: 'other' } })
  expect((screen.getByRole('button', { name: 'End cycle' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Comment (required)'), { target: { value: 'Role finished' } })
  fireEvent.click(screen.getByRole('button', { name: 'End cycle' }))
  await waitFor(() => expect(change).toHaveBeenCalledWith('demand', 1, 'end', { outcome: 'filled', reason_code: 'other', comment: 'Role finished', expected_updated_at: '2026-11-18', expected_event_id: 4 }))
  await waitFor(() => expect(close).toHaveBeenCalled())
})

it('shows stale-state errors without closing the dialog', async () => {
  vi.spyOn(api.maintenance, 'change').mockRejectedValue(new Error('Record changed; reload the record'))
  const close = vi.fn()
  show(<LifecycleDialog entity="resource" id={2} detail={detail} reopen onClose={close} />)
  await screen.findByRole('option', { name: 'Correction' })
  fireEvent.change(screen.getByLabelText('Reason (required)'), { target: { value: 'correction' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
  expect(await screen.findByText('Record changed; reload the record')).toBeTruthy()
  expect(close).not.toHaveBeenCalled()
})

it('renders empty history and archived read-only banner without end/reopen', async () => {
  vi.spyOn(api.maintenance, 'detail').mockResolvedValue({ ...detail, lifecycle: 'archived', lifecycle_events: [{ ...detail.lifecycle_events[0], to_state: 'archived' }] })
  show(<LifecycleControls entity="resource" id={2} showHistory />)
  expect(await screen.findByText('Archived on 18 Nov 2026 · Read-only')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'End of cycle…' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Reopen' })).toBeNull()
})

it('renders empty lifecycle history', () => {
  show(<LifecycleHistory detail={{ ...detail, lifecycle_events: [] }} />)
  expect(screen.getByText('No lifecycle events recorded.')).toBeTruthy()
})

it('loads row lifecycle detail only when an action is requested', async () => {
  const load = vi.spyOn(api.maintenance, 'detail').mockResolvedValue(detail)
  show(<LifecycleControls entity="demand" id={1} defer />)
  expect(load).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'End of cycle…' }))
  expect(await screen.findByRole('dialog', { name: 'End of cycle…' })).toBeTruthy()
  expect(load).toHaveBeenCalledOnce()
})

it('paginates ended records and displays the empty last page', async () => {
  const ended = vi.spyOn(api.maintenance, 'ended').mockResolvedValue({ items: [], total: 26 })
  show(<EndedRecords entity="demand" />)
  fireEvent.click(screen.getByRole('button', { name: 'Ended demands' }))
  await screen.findByText('No ended records.')
  fireEvent.click(screen.getByRole('button', { name: 'Next ended records' }))
  await waitFor(() => expect(ended).toHaveBeenLastCalledWith('demand', 2))
  expect(await screen.findByText('26 ended records · Page 2')).toBeTruthy()
})

it('keeps archived candidate profile read-only, including summaries and classification', async () => {
  await seed()
  await signIn('Admin', ADMIN.email, ADMIN.password)
  const resource = (await mock.listResources()).find((row) => row.candidateId)!
  vi.spyOn(api.maintenance, 'detail').mockResolvedValue({ ...detail, lifecycle: 'archived', lifecycle_events: [{ ...detail.lifecycle_events[0], to_state: 'archived' }] })
  renderApp(`/candidates/${resource.candidateId}`)
  expect(await screen.findByText('Archived on 18 Nov 2026 · Read-only')).toBeTruthy()
  expect((screen.getByLabelText('Override classification') as HTMLSelectElement).closest('fieldset')?.disabled).toBe(true)
  expect(screen.queryByRole('button', { name: 'Generate summary' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'End of cycle…' })).toBeNull()
})

it('searches archived resources with masked Show conventions and empty search results', async () => {
  const search = vi.spyOn(api.maintenance, 'search').mockResolvedValue({ items: [], total: 0, page: 1, page_size: 25 })
  show(<ArchivePage />)
  expect(await screen.findByText('No archived records match your search.')).toBeTruthy()
  search.mockResolvedValue({ items: [{ resource_id: 2, short_name: 'Demo Morgan', email_address: 'de**@example.test', archived_at: '2026-11-18', end_outcome: 'released' }], total: 1, page: 1, page_size: 25 })
  fireEvent.click(screen.getByRole('tab', { name: 'Resources' }))
  expect(await screen.findByText('de**@example.test')).toBeTruthy()
  vi.spyOn(api, 'unmask').mockResolvedValue('demo@example.test')
  fireEvent.click(screen.getByRole('button', { name: 'Show email' }))
  expect(await screen.findByText('demo@example.test')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Search archive'), { target: { value: 'Morgan' } })
  await waitFor(() => expect(search).toHaveBeenLastCalledWith('resources', expect.objectContaining({ q: 'Morgan', page: 1 })))
})

it('opens the archived resource itself even when it shares a candidate with an active record', async () => {
  vi.spyOn(api.maintenance, 'search').mockResolvedValue({ items: [{ resource_id: 2, candidate_id: 9, short_name: 'Historical resource', archived_at: '2026-11-18', end_outcome: 'released' }], total: 1, page: 1, page_size: 25 })
  const load = vi.spyOn(api.maintenance, 'detail').mockResolvedValue({ ...detail, short_name: 'Historical resource', lifecycle: 'archived', lifecycle_events: [{ ...detail.lifecycle_events[0], to_state: 'archived', comment: 'History of resource 2' }] })
  show(<ArchivePage />)
  fireEvent.click(screen.getByRole('tab', { name: 'Resources' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Read-only detail' }))
  const dialog = screen.getByRole('dialog', { name: 'Archived record — read-only' })
  expect(await within(dialog).findByText('Historical resource')).toBeTruthy()
  expect(load).toHaveBeenCalledWith('resource', 2)
  expect(within(dialog).getByText('History of resource 2')).toBeTruthy()
  expect(within(dialog).queryByRole('button', { name: 'End of cycle…' })).toBeNull()
  expect(within(dialog).queryByRole('button', { name: 'Reopen' })).toBeNull()
})

it('surfaces archive search errors', async () => {
  vi.spyOn(api.maintenance, 'search').mockRejectedValue(new Error('Service unavailable'))
  show(<ArchivePage />)
  expect(await screen.findByText('Service unavailable')).toBeTruthy()
})

it('updates inclusion hash, confirms exact archive counts, and submits selected batch', async () => {
  vi.spyOn(api.maintenance, 'preview').mockResolvedValue(batch)
  const updated = { ...batch, preview_hash: 'hash2', items: batch.items.map((i) => ({ ...i, included: i.entity_type === 'resource' ? 0 : 1 })) }
  vi.spyOn(api.maintenance, 'select').mockResolvedValue(updated)
  const run = vi.spyOn(api.maintenance, 'run').mockResolvedValue({ job_id: 9 })
  vi.spyOn(api.maintenance, 'batch').mockResolvedValue({ ...updated, status: 'completed', result: { demand: 1, resource: 0 } })
  show(<OccArchival />)
  fireEvent.click(screen.getByRole('button', { name: 'Build archive preview' }))
  expect(await screen.findByText('Filled — manual')).toBeTruthy()
  fireEvent.click(await screen.findByRole('tab', { name: 'Resources (1)' }))
  fireEvent.click(screen.getByRole('checkbox', { name: 'Include Resource 2' }))
  expect((screen.getByRole('checkbox', { name: 'Include Resource 2' }) as HTMLInputElement).checked).toBe(false)
  fireEvent.click(await screen.findByRole('button', { name: 'Archive 1 record' }))
  expect(screen.getByText('Archive exactly 1 demand and 0 resources from this preview?')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm archive' }))
  await waitFor(() => expect(run).toHaveBeenCalledWith(updated))
  expect(await screen.findByText(/Archive completed/)).toBeTruthy()
})

it('requires explicit housekeeping selection and confirmation', async () => {
  const cleanup = vi.spyOn(api.maintenance, 'cleanup').mockResolvedValue({ job_id: 10 })
  show(<OccArchival />)
  await screen.findByText('No housekeeping backups yet.')
  expect((screen.getByRole('button', { name: 'Run housekeeping' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByLabelText('Create backup'))
  fireEvent.click(screen.getByRole('button', { name: 'Run housekeeping' }))
  const dialog = screen.getByRole('dialog', { name: 'Confirm housekeeping' })
  expect(within(dialog).getByText('Create backup')).toBeTruthy()
  expect(cleanup).not.toHaveBeenCalled()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm housekeeping' }))
  await waitFor(() => expect(cleanup).toHaveBeenCalledWith(expect.objectContaining({ backup: true, purge_temp: false })))
})

it('disables an empty archive preview', async () => {
  vi.spyOn(api.maintenance, 'preview').mockResolvedValue({ ...batch, items: [] })
  show(<OccArchival />)
  fireEvent.click(screen.getByRole('button', { name: 'Build archive preview' }))
  expect(await screen.findByText('No eligible records found.')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Archive 0 records' }) as HTMLButtonElement).disabled).toBe(true)
})

it('confirms irreversible purge counts and shows execution errors', async () => {
  vi.spyOn(api.maintenance, 'preview').mockResolvedValue({ ...batch, kind: 'purge' })
  vi.spyOn(api.maintenance, 'run').mockRejectedValue(new Error('Preview changed; build a new preview'))
  show(<OccArchival />)
  fireEvent.click(screen.getByRole('button', { name: 'Build purge preview' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Purge 2 records' }))
  const dialog = screen.getByRole('dialog', { name: 'Confirm purge' })
  expect(within(dialog).getByText(/permanently deletes/)).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm purge' }))
  expect(await within(dialog).findByText('Preview changed; build a new preview')).toBeTruthy()
})

it('validates and saves retention settings', async () => {
  const save = vi.spyOn(api.maintenance, 'saveSettings').mockResolvedValue({ 'archive.retention_months': 18 })
  show(<OccArchival />)
  fireEvent.change(screen.getByLabelText('Retention (months)'), { target: { value: '0' } })
  expect((screen.getByRole('button', { name: 'Save retention' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Retention (months)'), { target: { value: '18' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save retention' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith({ 'archive.retention_months': 18 }))
})
