import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, ANALYST_PASSWORD, mock, renderApp, seed, signIn } from '../../test/utils'

describe('OCC cached CV text', () => {
  beforeEach(async () => { await seed(); await signIn('Admin', ADMIN.email, ADMIN.password) })
  afterEach(() => vi.restoreAllMocks())

  async function open() {
    const resource = (await mock.listResources()).find((r) => r.cvCount > 0)!
    renderApp('/occ/cv-extractions')
    const select = await screen.findByLabelText('Candidate')
    await screen.findByRole('option', { name: new RegExp(`#${resource.candidateId}$`) })
    fireEvent.change(select, { target: { value: resource.candidateId } })
    await screen.findByRole('table', { name: 'Cached CV extractions' })
    return resource.candidateId!
  }

  it('lists versions and removed flags, cancels safely, then clears and shows every returned count', async () => {
    const spy = vi.spyOn(mock, 'clearCandidateExtractions')
    const id = await open()
    const versions = await mock.listCvExtractions(id)
    expect(screen.getByText(versions[0].fileName)).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Extracted at' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: "Clear this candidate's extractions" }))
    const dialog = within(screen.getByRole('dialog'))
    expect(dialog.getByText(/Original CV files, candidate identities and past match runs are kept/)).toBeTruthy()
    expect(dialog.getByText(/The next CV sync re-reads the files/)).toBeTruthy()
    expect(spy).not.toHaveBeenCalled()
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }))
    expect(spy).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: "Clear this candidate's extractions" }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear' }))
    expect(await screen.findByText(new RegExp(`Versions cleared: ${versions.length} · Chunks deleted: ${versions.length} · Vectors deleted: ${versions.length} · Matching vectors deleted: ${versions.length}`))).toBeTruthy()
    expect(spy).toHaveBeenCalledWith(id)
    await waitFor(() => expect(screen.getAllByText('Yes').length).toBe(versions.length))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('confirms all-candidate removal and displays distinct backend counts', async () => {
    const spy = vi.spyOn(mock, 'clearAllExtractions').mockResolvedValue({ versionsCleared: 4, chunksDeleted: 5, vectorsDeleted: 6, matchingVectorsDeleted: 7 })
    await open()
    fireEvent.click(screen.getByRole('button', { name: "Clear all candidates' extractions" }))
    expect(screen.getByRole('dialog', { name: 'Clear all cached CV text?' })).toBeTruthy()
    expect(spy).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear' }))
    expect(await screen.findByText('Versions cleared: 4 · Chunks deleted: 5 · Vectors deleted: 6 · Matching vectors deleted: 7')).toBeTruthy()
    expect(spy).toHaveBeenCalledOnce()
  })

  it('keeps errors in the dialog and allows cancellation', async () => {
    vi.spyOn(mock, 'clearAllExtractions').mockRejectedValue(new Error('Removal unavailable'))
    await open()
    fireEvent.click(screen.getByRole('button', { name: "Clear all candidates' extractions" }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear' }))
    expect(await screen.findByText('Removal unavailable')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows an empty list and handles a list error', async () => {
    vi.spyOn(mock, 'listCvExtractions').mockResolvedValueOnce([]).mockRejectedValue(new Error('Listing unavailable'))
    const rows = await mock.listResources()
    renderApp('/occ/cv-extractions')
    const select = await screen.findByLabelText('Candidate')
    await screen.findByRole('option', { name: new RegExp(`#${rows[0].candidateId}$`) })
    fireEvent.change(select, { target: { value: rows[0].candidateId } })
    expect(await screen.findByText('No cached CV versions for this candidate.')).toBeTruthy()
    fireEvent.change(select, { target: { value: rows[1].candidateId } })
    expect(await screen.findByText('Listing unavailable')).toBeTruthy()
  })

  it('does not expose OCC controls to an Analyst', async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
    renderApp('/occ/cv-extractions')
    expect(await screen.findByRole('heading', { name: "You don't have access to this area" })).toBeTruthy()
    expect(screen.queryByRole('button', { name: "Clear all candidates' extractions" })).toBeNull()
  })

  it('mock removal retains identity and versions, is idempotent and refills on sync', async () => {
    const resource = (await mock.listResources()).find((r) => r.cvCount > 0)!
    const id = resource.candidateId!
    const before = await mock.getCandidate(id)
    await mock.clearAllExtractions()
    expect((await mock.listCvExtractions(id)).every((v) => v.removed)).toBe(true)
    expect((await mock.getCandidate(id)).internalRef).toBe(before.internalRef)
    expect((await mock.getCandidate(id)).cvVersions.map((v) => v.cvVersionId)).toEqual(before.cvVersions.map((v) => v.cvVersionId))
    expect((await mock.clearCandidateExtractions(id)).versionsCleared).toBe(0)
    await mock.syncCvs([resource.resourceId])
    expect((await mock.listCvExtractions(id)).every((v) => !v.removed)).toBe(true)
    expect((await mock.getCandidate(id)).cvVersions[0].facts).toBeTruthy()
  })
})
