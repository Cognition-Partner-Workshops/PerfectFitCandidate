import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, ANALYST_PASSWORD, mock, renderApp, seed, signIn } from '../test/utils'

describe('Candidate classification', () => {
  beforeEach(async () => { await seed(); await signIn('Admin', ADMIN.email, ADMIN.password) })
  afterEach(() => vi.restoreAllMocks())

  async function open() {
    const id = (await mock.listResources())[0].candidateId!
    renderApp(`/candidates/${id}`)
    return { id, card: within(await screen.findByRole('region', { name: 'Candidate classification' })) }
  }

  it('renders live values, requires a nonblank reason to set and clear, and refreshes metadata', async () => {
    const spy = vi.spyOn(mock, 'setClassification')
    const { id, card } = await open()
    expect(card.getByText(/Effective classification/)).toBeTruthy()
    expect(card.getByText(/Computed classification/)).toBeTruthy()
    const reason = card.getByLabelText(/Classification reason/)
    const set = card.getByRole('button', { name: 'Set override' }) as HTMLButtonElement
    expect(set.disabled).toBe(true)
    fireEvent.change(reason, { target: { value: '   ' } })
    expect(set.disabled).toBe(true)
    fireEvent.change(card.getByLabelText('Override classification'), { target: { value: 'Unknown' } })
    fireEvent.change(reason, { target: { value: 'Checked private@example.com' } })
    fireEvent.click(set)
    expect(await card.findByText('Reason: Checked [REDACTED]')).toBeTruthy()
    expect(card.getByText(/Set by user #/)).toBeTruthy()
    expect(spy).toHaveBeenCalledWith(id, { classification: 'Unknown', reason: 'Checked private@example.com' })
    const clear = card.getByRole('button', { name: 'Clear override' }) as HTMLButtonElement
    expect(clear.disabled).toBe(true)
    fireEvent.change(reason, { target: { value: 'Return to automatic' } })
    fireEvent.click(clear)
    await waitFor(() => expect(card.queryByText(/Reason: Checked/)).toBeNull())
    expect((await mock.getCandidate(id)).classification.classificationOverride).toBeNull()
    expect(spy).toHaveBeenLastCalledWith(id, { classification: null, reason: 'Return to automatic' })
  })

  it('shows failures without losing the reason', async () => {
    vi.spyOn(mock, 'setClassification').mockRejectedValue(new Error('Save unavailable'))
    const { card } = await open()
    fireEvent.change(card.getByLabelText(/Classification reason/), { target: { value: 'Reviewed' } })
    fireEvent.click(card.getByRole('button', { name: 'Set override' }))
    expect(await card.findByText('Save unavailable')).toBeTruthy()
    expect((card.getByLabelText(/Classification reason/) as HTMLTextAreaElement).value).toBe('Reviewed')
  })

  it('shows Analysts the values and metadata read-only', async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Admin', ADMIN.email, ADMIN.password)
    const id = (await mock.listResources())[0].candidateId!
    await mock.setClassification(id, { classification: 'External', reason: 'Admin reviewed' })
    await mock.logout()
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
    renderApp(`/candidates/${id}`)
    const card = within(await screen.findByRole('region', { name: 'Candidate classification' }))
    expect(card.getByText('Reason: Admin reviewed')).toBeTruthy()
    expect(card.getByText(/Computed classification/)).toBeTruthy()
    expect(card.queryByRole('button')).toBeNull()
    expect(card.queryByRole('textbox')).toBeNull()
    expect(card.queryByRole('combobox')).toBeNull()
  })
})
