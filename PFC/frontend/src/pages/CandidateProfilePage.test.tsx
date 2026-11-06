import { fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CandidateDetail, CvFacts } from '../api/types'
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
  }

  const open = async (detail: (d: CandidateDetail) => CandidateDetail) => {
    const id = (await mock.listResources()).find((r) => r.cvCount > 0)!.candidateId
    const base = await mock.getCandidate(id)
    vi.spyOn(mock, 'getCandidate').mockResolvedValue(detail(base))
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
    expect(screen.getAllByText('Not found in CV').length).toBe(2)
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
    expect(screen.getAllByText('Not found in CV').length).toBe(5)
    expect(screen.getByText(/CV-999: synced before CV facts were extracted/)).toBeTruthy()
    expect(base.cvVersions.length).toBeGreaterThan(0)
  })

  it('mock gives the latest CV masked facts and leaves older versions without facts', async () => {
    const id = (await mock.listResources()).find((r) => r.cvCount > 1)!.candidateId
    const [latest, older] = (await mock.getCandidate(id)).cvVersions
    expect(latest.facts?.phone?.value.startsWith('***')).toBe(true)
    expect(latest.facts?.qualifications).toEqual([])
    expect(older.facts).toBeNull()
  })
})
