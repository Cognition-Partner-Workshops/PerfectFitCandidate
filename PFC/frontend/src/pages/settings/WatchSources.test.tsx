import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../../test/utils'
import { ApiError } from '../../api'
import type { WatchRun } from '../../api/types'

const run: WatchRun = { runId: 44, fileName: 'CT_review.xlsx', status: 'preview', startedAt: '2026-01-01T12:00:00Z', committedAt: null, counts: { read: 2, new: 2, updated: 0, unchanged: 0, rejected: 0, incomplete: 0 } }

describe('watch data sources', () => {
  beforeEach(async () => { await seed(); await signIn('Admin', ADMIN.email, ADMIN.password) })
  afterEach(() => vi.restoreAllMocks())

  it('enables and saves the path, glob, interval and hold preference', async () => {
    const save = vi.spyOn(mock, 'saveMySettings')
    renderApp('/settings/data')
    const source = within(await screen.findByRole('group', { name: 'Auto-sync — Demand List' }))
    fireEvent.click(source.getByLabelText('Enabled'))
    fireEvent.change(source.getByLabelText('Watch path'), { target: { value: '/synced/dl' } })
    fireEvent.change(source.getByLabelText('File pattern'), { target: { value: 'Demand*.xlsx' } })
    fireEvent.change(source.getByLabelText('Poll every N minutes'), { target: { value: '2' } })
    fireEvent.click(source.getByLabelText('Hold for my review before committing'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText('Saved')
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ autoSyncDl: { enabled: true, watchPath: '/synced/dl', pattern: 'Demand*.xlsx', pollMinutes: 2, requirePreview: true } }))
    expect((await mock.getMySettings()).autoSyncDl.enabled).toBe(true)
  })

  it.each(['WATCH_PATH_INVALID', 'WATCH_PATTERN_INVALID'])('shows %s without losing edits', async (code) => {
    vi.spyOn(mock, 'saveMySettings').mockRejectedValue(new ApiError({ status: 422, title: 'Check the watch folder', code }))
    renderApp('/settings/data')
    const source = within(await screen.findByRole('group', { name: 'Auto-sync — Demand List' }))
    fireEvent.change(source.getByLabelText('Watch path'), { target: { value: 'bad path' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Check the watch folder')).toBeTruthy()
    expect((source.getByLabelText('Watch path') as HTMLInputElement).value).toBe('bad path')
  })

  it('bounds the poll interval and displays empty status', async () => {
    renderApp('/settings/data')
    const source = within(await screen.findByRole('group', { name: 'Auto-sync — Demand List' }))
    expect(await source.findByText('No files awaiting review.')).toBeTruthy()
    fireEvent.change(source.getByLabelText('Poll every N minutes'), { target: { value: '0' } })
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(source.getByLabelText('Poll every N minutes'), { target: { value: '1440' } })
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('renders status and opens an existing preview to commit and refresh', async () => {
    const empty = (await mock.getWatchStatus()).dl
    const status = vi.spyOn(mock, 'getWatchStatus').mockResolvedValue({ dl: { ...empty, reachable: false, lastScanAt: run.startedAt, lastError: 'Watch folder is not accessible', pending: [{ fileName: 'next.xlsx', reason: 'locked' }], lastPickup: run }, ct: { ...empty, awaitingReview: [run] } })
    const load = vi.spyOn(mock, 'getIngestionPreview').mockResolvedValue({ ingestionRunId: 44, kind: 'ct', fileName: run.fileName, sheet: 'Candidates', rowsRead: 2, new: 2, updated: 0, unchanged: 0, rejected: 0, warnings: [], alreadyImported: false })
    const commit = vi.spyOn(mock, 'commitIngestion')
    renderApp('/settings/data')
    expect(await screen.findByText('Watch folder is not accessible')).toBeTruthy()
    expect(screen.getByText('next.xlsx — locked')).toBeTruthy()
    expect(screen.getByText(/Last scan: 01 Jan 2026 12:00/)).toBeTruthy()
    expect(screen.getByText(/Rows read 2 · New 2/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Review' }))
    const dialog = within(await screen.findByRole('dialog'))
    expect(await dialog.findByText(/Sheet detected/)).toBeTruthy()
    expect(load).toHaveBeenCalledWith(44)
    expect(dialog.queryByLabelText('Candidate Tracker workbook')).toBeNull()
    fireEvent.click(dialog.getByRole('button', { name: 'Commit 2 rows' }))
    await waitFor(() => expect(commit).toHaveBeenCalledWith(44))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(status.mock.calls.length).toBeGreaterThan(1)
  })

  it('shows a status fetch error without hiding the settings controls', async () => {
    vi.spyOn(mock, 'getWatchStatus').mockRejectedValue(new ApiError({ status: 503, title: 'Pickup status unavailable', code: 'UNAVAILABLE' }))
    renderApp('/settings/data')
    expect(await screen.findByText('Pickup status unavailable')).toBeTruthy()
    expect(screen.getAllByLabelText('Watch path')).toHaveLength(2)
  })

  it('shows a failed existing preview load and allows closing it', async () => {
    const empty = (await mock.getWatchStatus()).dl
    vi.spyOn(mock, 'getWatchStatus').mockResolvedValue({ dl: empty, ct: { ...empty, awaitingReview: [run] } })
    vi.spyOn(mock, 'getIngestionPreview').mockRejectedValue(new ApiError({ status: 404, title: 'Preview unavailable', code: 'NOT_FOUND' }))
    renderApp('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: 'Review' }))
    const dialog = within(await screen.findByRole('dialog'))
    expect(await dialog.findByText('Preview unavailable')).toBeTruthy()
    fireEvent.click(dialog.getAllByRole('button', { name: /^Close$/ })[0])
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps the dialog open after a failed discard and refreshes after retry', async () => {
    const empty = (await mock.getWatchStatus()).dl
    const status = vi.spyOn(mock, 'getWatchStatus').mockResolvedValue({ dl: empty, ct: { ...empty, awaitingReview: [run] } })
    vi.spyOn(mock, 'getIngestionPreview').mockResolvedValue({ ingestionRunId: 44, kind: 'ct', fileName: run.fileName, sheet: 'Candidates', rowsRead: 0, new: 0, updated: 0, unchanged: 0, rejected: 0, warnings: [], alreadyImported: false })
    const discard = vi.spyOn(mock, 'discardIngestion').mockRejectedValueOnce(new ApiError({ status: 409, title: 'Please retry discard', code: 'CONFLICT' })).mockResolvedValueOnce(undefined)
    renderApp('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: 'Review' }))
    const dialog = within(await screen.findByRole('dialog'))
    fireEvent.click(await dialog.findByRole('button', { name: 'Discard' }))
    expect(await dialog.findByText('Please retry discard')).toBeTruthy()
    fireEvent.click(dialog.getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(discard).toHaveBeenCalledWith(44)
    expect(status.mock.calls.length).toBeGreaterThan(1)
  })
})
