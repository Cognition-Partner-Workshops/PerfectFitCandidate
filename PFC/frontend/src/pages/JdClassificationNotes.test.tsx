import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { JdRequirementItem } from '../api/types'
import { ANALYST_PASSWORD, mock, renderApp, seed, signIn } from '../test/utils'

describe('JD requirement classification provenance', () => {
  beforeEach(async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
  })
  afterEach(() => vi.restoreAllMocks())

  async function open(items?: JdRequirementItem[]) {
    const base = await mock.getRequirements(1)
    vi.spyOn(mock, 'getRequirements').mockResolvedValue({ ...base, mandatorySkills: ['SQL', 'Python'], preferredSkills: ['SQL'], items })
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
  }

  it.each([
    ['mandatory_skill', 'Mandatory', 'Preferred'],
    ['preferred_skill', 'Preferred', 'Mandatory'],
  ] as const)('shows the correct note for %s and not the same text in the other kind', async (kind, target, other) => {
    await open([{ kind, text: ' ＳＱＬ ', classificationReason: 'classification_adjusted' }])
    const group = within(screen.getByRole('group', { name: `${target} skills: SQL` }))
    expect(group.getByRole('note').textContent).toBe(`Moved to ${target} based on JD wording`)
    expect(within(screen.getByRole('group', { name: `${other} skills: SQL` })).queryByRole('note')).toBeNull()
    expect(screen.queryByText(`Moved to ${other} based on JD wording`)).toBeNull()
  })

  it('keeps adjusted and evidence-flagged skills editable and confirmable with unchanged items', async () => {
    const items: JdRequirementItem[] = [{ kind: 'mandatory_skill', text: 'SQL', jdReqId: 8, classificationReason: 'classification_adjusted', evidenceUnverified: true }]
    await open(items)
    const save = vi.spyOn(mock, 'saveRequirements')
    const confirm = vi.spyOn(mock, 'confirmRequirements')
    expect(screen.getByText('Evidence not found in JD text — check before confirming')).toBeTruthy()
    expect(screen.getByText('Moved to Mandatory based on JD wording')).toBeTruthy()
    const add = screen.getByLabelText('Add mandatory skills')
    fireEvent.change(add, { target: { value: 'Rust' } })
    fireEvent.keyDown(add, { key: 'Enter' })
    expect(within(screen.getByRole('group', { name: 'Mandatory skills: Rust' })).queryByRole('note')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Confirm & Start/ }))
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(1))
    expect(save.mock.calls[0][0].items).toEqual(items)
    expect(save.mock.calls[0][0].mandatorySkills).toContain('Rust')
  })

  it('does not attach adjustment notes to a removed and re-added chip', async () => {
    await open([{ kind: 'mandatory_skill', text: 'Python', classificationReason: 'classification_adjusted' }])
    fireEvent.click(screen.getByRole('button', { name: 'Remove Python' }))
    const add = screen.getByLabelText('Add mandatory skills')
    fireEvent.change(add, { target: { value: 'Python' } })
    fireEvent.keyDown(add, { key: 'Enter' })
    expect(screen.queryByText(/Moved to/)).toBeNull()
  })

  it.each([undefined, [], [{ kind: 'mandatory_skill', text: 'SQL', classificationReason: null }]] as const)('shows no note for legacy or unadjusted items: %j', async (items) => {
    await open(items ? [...items] : undefined)
    expect(screen.queryByText(/Moved to/)).toBeNull()
  })

  it('generates adjusted mock defaults for both skill kinds and preserves them on save', async () => {
    const req = await mock.getRequirements(1)
    for (const kind of ['mandatory_skill', 'preferred_skill']) {
      expect(req.items?.some((item) => item.kind === kind && item.classificationReason === 'classification_adjusted')).toBe(true)
    }
    expect((await mock.saveRequirements(req)).items).toEqual(req.items)
  })
})
