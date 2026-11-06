import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { JdRequirementItem, JdRequirements } from '../api/types'
import { ANALYST_PASSWORD, mock, renderApp, seed, signIn } from '../test/utils'

const warning = 'Evidence not found in JD text — check before confirming'

describe('JD requirement evidence', () => {
  beforeEach(async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
  })
  afterEach(() => vi.restoreAllMocks())

  async function open(req: Partial<JdRequirements> = {}) {
    const base = await mock.getRequirements(1)
    const read = vi.spyOn(mock, 'getRequirements').mockResolvedValue({ ...base, mandatorySkills: ['SQL', 'Python'], preferredSkills: ['SQL'], items: [], ...req })
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
    return read
  }

  it('matches exact text and kind and renders accessible text only for flagged items', async () => {
    await open({ items: [
      { kind: 'mandatory_skill', text: 'SQL', evidence: null, evidenceUnverified: true },
      { kind: 'preferred_skill', text: 'SQL', evidence: 'SQL is preferred', evidenceUnverified: false },
      { kind: 'mandatory_skill', text: 'Python' },
    ] })
    expect(within(screen.getByRole('group', { name: 'Mandatory skills: SQL' })).getByRole('note').textContent).toBe(warning)
    expect(within(screen.getByRole('group', { name: 'Preferred skills: SQL' })).queryByRole('note')).toBeNull()
    expect(within(screen.getByRole('group', { name: 'Mandatory skills: Python' })).queryByRole('note')).toBeNull()
  })

  it('still edits and confirms flagged requirements, sending the original items unchanged', async () => {
    const items: JdRequirementItem[] = [{ kind: 'mandatory_skill', text: 'SQL', jdReqId: 42, evidence: null, evidenceUnverified: true }]
    await open({ items })
    const save = vi.spyOn(mock, 'saveRequirements')
    const confirm = vi.spyOn(mock, 'confirmRequirements')
    const add = screen.getByLabelText('Add preferred skills')
    fireEvent.change(add, { target: { value: 'Rust' } })
    fireEvent.keyDown(add, { key: 'Enter' })
    expect(within(screen.getByRole('group', { name: 'Preferred skills: Rust' })).queryByRole('note')).toBeNull()
    expect(screen.getByText(warning)).toBeTruthy()
    const button = screen.getByRole('button', { name: /Confirm & Start/ }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
    fireEvent.click(button)
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(1))
    expect(save.mock.calls[0][0].items).toEqual(items)
    expect(save.mock.calls[0][0].preferredSkills).toContain('Rust')
  })

  it('keeps the warning for an identically re-added chip before save and after reopening', async () => {
    const items: JdRequirementItem[] = [{ kind: 'mandatory_skill', text: 'SQL', evidenceUnverified: true }]
    await open({ preferredSkills: [], items })
    const save = vi.spyOn(mock, 'saveRequirements')
    fireEvent.click(screen.getByRole('button', { name: 'Remove SQL' }))
    expect(screen.queryByText(warning)).toBeNull()
    const add = screen.getByLabelText('Add mandatory skills')
    fireEvent.change(add, { target: { value: 'SQL' } })
    fireEvent.keyDown(add, { key: 'Enter' })
    expect(screen.getByText(warning)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(save).toHaveBeenCalledOnce())
    expect(save.mock.calls[0][0].items).toEqual(items)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save draft' })).toBeNull())
    fireEvent.click(screen.getAllByRole('button', { name: 'Review JD' })[0])
    expect(await screen.findByText(warning)).toBeTruthy()
  })

  it.each(['qualification', 'domain'] as const)('drops a case-edited %s warning before save and after reopening', async (kind) => {
    const field = kind === 'qualification' ? 'qualifications' : 'domain'
    const label = kind === 'qualification' ? /Qualifications/ : 'Domain'
    const text = kind === 'qualification' ? 'MSc' : 'Insurance'
    const items: JdRequirementItem[] = [{ kind, text, jdReqId: 42, evidenceUnverified: true }]
    const read = await open({ [field]: text, items })
    const save = vi.spyOn(mock, 'saveRequirements').mockImplementation(async (req) => {
      const persisted = { ...req, items: [{ kind, text: req[field], evidence: null, evidenceUnverified: false }] }
      read.mockResolvedValue(persisted)
      return persisted
    })
    expect(screen.getByText(warning)).toBeTruthy()
    fireEvent.change(screen.getByLabelText(label), { target: { value: text.toLowerCase() } })
    expect(screen.queryByText(warning)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(save).toHaveBeenCalledOnce())
    expect(save.mock.calls[0][0][field]).toBe(text.toLowerCase())
    expect(save.mock.calls[0][0].items).toEqual(items)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save draft' })).toBeNull())
    fireEvent.click(screen.getAllByRole('button', { name: 'Review JD' })[0])
    await waitFor(() => expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe(text.toLowerCase()))
    expect(screen.queryByText(warning)).toBeNull()
  })

  it.each([
    ['SQL', 'sql'],
    ['SQL', ' SQL '],
    ['SQL', 'ＳＱＬ'],
    ['SQL Server', 'SQL  Server'],
  ])('does not normalize skill metadata %s to %s', async (text, edited) => {
    await open({ mandatorySkills: [edited], items: [{ kind: 'mandatory_skill', text, evidenceUnverified: true }] })
    expect(screen.queryByText(warning)).toBeNull()
  })

  it('shows warnings beside matching experience, qualification and domain rows', async () => {
    await open({ experience: ['5 years SQL'], qualifications: 'BSc;  MSc ', domain: ' Insurance ', items: [
      { kind: 'experience', text: '5 years SQL', evidenceUnverified: true },
      { kind: 'qualification', text: ' MSc ', evidenceUnverified: true },
      { kind: 'domain', text: 'Insurance', evidenceUnverified: true },
    ] })
    expect(screen.getAllByText(warning)).toHaveLength(3)
    fireEvent.change(screen.getByLabelText(/Qualifications/), { target: { value: 'PhD' } })
    expect(screen.getAllByText(warning)).toHaveLength(2)
  })

  it('supports older responses with no items', async () => {
    await open({ items: undefined })
    expect(screen.queryByText(warning)).toBeNull()
    expect((screen.getByRole('button', { name: /Confirm & Start/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('generates flagged mock defaults and preserves metadata on save', async () => {
    const req = await mock.getRequirements(1)
    expect(req.items?.some((item) => item.evidenceUnverified)).toBe(true)
    expect((await mock.saveRequirements(req)).items).toEqual(req.items)
  })
})
