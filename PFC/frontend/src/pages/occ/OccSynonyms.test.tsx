import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { ADMIN, mock, renderApp, seed, signIn } from '../../test/utils'

describe('OCC skill suggestions', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  async function openSuggestions() {
    renderApp('/occ/synonyms')
    fireEvent.click(await screen.findByRole('tab', { name: /Suggestions/ }))
    await screen.findByRole('table')
  }

  it('limits Select all and bulk add to the currently filtered rows', async () => {
    const accept = vi.spyOn(mock, 'acceptSkillSuggestions')
    await openSuggestions()
    fireEvent.change(screen.getByLabelText('Search suggestions'), { target: { value: 'SQL Server' } })
    const row = screen.getByRole('row', { name: /SQL Server/ })
    expect(row).toBeTruthy()
    expect(within(row).getByText(/JD: \d+ demands/)).toBeTruthy()
    expect(screen.queryByRole('row', { name: /Kubernetes/ })).toBeNull()
    fireEvent.click(screen.getByLabelText('Select all visible suggestions'))
    const add = screen.getByRole('button', { name: 'Add selected as new skills (1)' })
    fireEvent.click(add)
    await waitFor(() => expect(accept).toHaveBeenCalledWith([{ term: 'SQL Server' }]))
  })

  it('rejects a suggestion and restores it from the Rejected view', async () => {
    const reject = vi.spyOn(mock, 'rejectSkillSuggestions')
    await openSuggestions()
    const row = await screen.findByRole('row', { name: /Kubernetes/ })
    fireEvent.click(within(row).getByRole('button', { name: 'Reject' }))
    await waitFor(() => expect(reject).toHaveBeenCalledWith(['Kubernetes']))
    fireEvent.click(screen.getByRole('tab', { name: /Rejected/ }))
    const rejected = await screen.findByRole('row', { name: /Kubernetes/ })
    fireEvent.click(within(rejected).getByRole('button', { name: 'Restore' }))
    fireEvent.click(screen.getByRole('tab', { name: /Suggestions/ }))
    expect(await screen.findByRole('row', { name: /Kubernetes/ })).toBeTruthy()
  })
})
