import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CandidateDetail, CvFacts } from '../api/types'
import { ApiError } from '../api'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

/** Candidate Profile CV facts (REQ-IN-21/23): grounded values with evidence, masked phone, no guessing. */
describe('Candidate Profile CV facts', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  const facts: CvFacts = {
    name: { value: 'Alex Alexander', evidence: 'Alex Alexander, Data Analyst', span: [0, 28], source: 'rule' },
    phone: { value: '***89', evidence: 'Mobile ***89', span: [30, 52], source: 'rule' },
    experience: [{ value: '6 years SQL', evidence: '6 years SQL across retail banking', span: [60, 93], source: 'llm' }],
    qualifications: [],
    projects: [],
    skills: [],
    warnings: [],
  }

  const open = async (detail: (d: CandidateDetail) => CandidateDetail) => {
    const id = (await mock.listResources()).find((r) => r.cvCount > 0)!.candidateId
    const base = await mock.getCandidate(id)
    vi.spyOn(mock, 'getCandidate').mockResolvedValue(detail({ ...base, identityStatus: 'complete' }))
    renderApp(`/candidates/${id}`)
    fireEvent.click(await screen.findByRole('tab', { name: /CV versions/ }, { timeout: 5000 }))
    return base
  }

  it('shows each fact with its source tag and expandable evidence, and empty lists as not found', async () => {
    await open((d) => ({ ...d, cvVersions: [{ ...d.cvVersions[0], facts }] }))
    expect(await screen.findByText(/CV facts —/)).toBeTruthy()
    expect(screen.getByText('6 years SQL')).toBeTruthy()
    expect(screen.getByText('Local AI')).toBeTruthy()
    expect(screen.getAllByText('Rule').length).toBe(2)
    expect(screen.getByText('***89')).toBeTruthy()
    expect(screen.getByText('6 years SQL across retail banking').closest('details')).toBeTruthy()
    expect(screen.getAllByText('Not found in CV').length).toBe(3)
  })

  it('shows "Not found in CV" for a missing name or phone and nothing for legacy versions', async () => {
    const base = await open((d) => ({
      ...d,
      cvVersions: [
        { ...d.cvVersions[0], facts: { ...facts, name: null, phone: null, experience: [] } },
        { ...d.cvVersions[0], cvVersionId: 999, facts: null },
      ],
    }))
    expect(await screen.findByText(/CV facts —/)).toBeTruthy()
    expect(screen.getAllByText('Not found in CV').length).toBe(6)
    expect(screen.getByText(/CV-999: CV facts not extracted yet/)).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(base.cvVersions.length).toBeGreaterThan(0)
  })

  it('mock gives the latest CV masked facts and leaves older versions without facts', async () => {
    const id = (await mock.listResources()).find((r) => r.cvCount > 1)!.candidateId
    const [latest, older] = (await mock.getCandidate(id)).cvVersions
    expect(latest.facts?.phone?.value.startsWith('***')).toBe(true)
    expect(latest.facts?.qualifications).toEqual([])
    expect(older.facts).toBeNull()
  })

  it('shows verbatim skills, differing canonicals, provenance and expandable evidence', async () => {
    await open((d) => ({ ...d, cvVersions: [{ ...d.cvVersions[0], facts: { ...facts, skills: [
      { value: 'BA', canonical: 'Business Analysis', evidence: 'BA', span: [101, 103], source: 'rule' },
      { value: 'SQL', canonical: 'SQL', evidence: 'SQL', span: [105, 108], source: 'llm' },
      { value: 'Unlisted', canonical: null, evidence: 'Unlisted', span: [110, 118], source: 'rule' },
    ] } }] }))
    const card = screen.getByRole('region', { name: /CV facts for/ })
    expect(within(card).getByText('Skills')).toBeTruthy()
    expect(within(card).getByText('→ Business Analysis')).toBeTruthy()
    expect(within(card).queryByText('→ SQL')).toBeNull()
    const evidence = within(card).getAllByText('BA').find((e) => e.tagName === 'BLOCKQUOTE')!
    const details = evidence.closest('details')!
    expect(details.open).toBe(false)
    fireEvent.click(within(details).getByText('Evidence'))
    expect(details.open).toBe(true)
    expect(within(card).getAllByText('Local AI')).toHaveLength(2)
    expect(within(card).getAllByText('Rule')).toHaveLength(4)
  })

  it('shows non-blocking card and latest-CV phone warnings', async () => {
    await open((d) => ({ ...d, cvVersions: [{ ...d.cvVersions[0], facts: { ...facts, phone: null,
      warnings: [{ code: 'PHONE_NOT_FOUND', message: 'Phone not found in CV.' }],
    } }] }))
    expect(screen.getByText('Latest CV: Phone not found in CV.').closest('[role="status"]')).toBeTruthy()
    expect(screen.getByText('Phone not found in CV.').closest('[role="status"]')).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('tab', { name: /Overview/ })).not.toHaveProperty('disabled', true)
  })

  it('does not warn at profile level for an older best CV or a legacy latest CV', async () => {
    await open((d) => ({ ...d, cvVersions: [
      { ...d.cvVersions[0], isBest: false, facts: null },
      { ...d.cvVersions[0], cvVersionId: 999, isBest: true, facts: { ...facts, phone: null,
        warnings: [{ code: 'PHONE_NOT_FOUND', message: 'Phone not found in CV.' }],
      } },
    ] }))
    expect(screen.queryByText(/Latest CV:/)).toBeNull()
    expect(screen.getByText('Phone not found in CV.')).toBeTruthy()
  })

  it.each(['incomplete', 'ambiguous'] as const)('auto-opens %s identity with internal-ID default', async (identityStatus) => {
    await open((d) => ({ ...d, identityStatus,
      ...(identityStatus === 'incomplete' ? {
        resource: { ...d.resource, resourceId: 0, name: null, emailAddressMasked: null, identityStatus },
        ctEmailOptions: [],
        cvVersions: [{ ...d.cvVersions[0], facts: { ...facts, name: null } }],
      } : {}),
    }))
    const dialog = screen.getByRole('dialog', { name: 'Incomplete Candidate Information' })
    expect(within(dialog).getByRole('radio', { name: /Generate internal ID and continue/ })).toHaveProperty('checked', true)
    expect(within(dialog).getByRole('radio', { name: 'Manual entry' })).toHaveProperty('checked', false)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([false, true])('submits manual identity with optional email=%s and displays 409 detail inline', async (withEmail) => {
    const detail = 'CT email remains authoritative'
    const resolve = vi.spyOn(mock, 'resolveIdentity').mockRejectedValueOnce(new ApiError({
      status: 409, title: 'Conflict', detail,
    }))
    const base = await open((d) => ({ ...d, identityStatus: 'incomplete' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Manual entry' }))
    const next = within(dialog).getByRole('button', { name: 'Continue' })
    expect(next).toHaveProperty('disabled', true)
    fireEvent.change(within(dialog).getByLabelText('Name (required)'), { target: { value: '   ' } })
    expect(next).toHaveProperty('disabled', true)
    fireEvent.change(within(dialog).getByLabelText('Name (required)'), { target: { value: ' Morgan Synthetic ' } })
    if (withEmail) fireEvent.change(within(dialog).getByLabelText('Email (optional)'), { target: { value: 'Manual.Identity@example.com' } })
    fireEvent.click(next)
    await waitFor(() => expect(resolve).toHaveBeenCalledWith(base.candidateId, {
      action: 'manual_entry', name: 'Morgan Synthetic', ...(withEmail ? { email: 'Manual.Identity@example.com' } : {}),
    }))
    expect(await within(dialog).findByRole('alert')).toHaveProperty('textContent', detail)
    expect(screen.getByRole('dialog')).toBeTruthy()
    resolve.mockResolvedValueOnce({ ...base, identityStatus: 'complete' })
    fireEvent.click(next)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})
