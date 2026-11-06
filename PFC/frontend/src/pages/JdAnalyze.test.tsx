import { fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

/** Analyze JD on a never-analysed demand extracts a draft before loading it (REQ-MAT-02). */
describe('Analyze JD', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  it('analyzes a new demand once and shows the draft it returns', async () => {
    const analyze = vi.spyOn(mock, 'analyzeDemand')
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Analyze JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(analyze).toHaveBeenCalledTimes(1)
  })

  it('does not re-analyze when a draft exists but the row still says Not analysed', async () => {
    const analyze = vi.spyOn(mock, 'analyzeDemand')
    const real = mock.getRequirements.bind(mock)
    vi.spyOn(mock, 'getRequirements').mockImplementation(async (id) => {
      try {
        return await real(id)
      } catch {
        return { demandId: id, mandatorySkills: ['Selenium'], preferredSkills: [], experienceYears: 3, qualifications: '', domain: '', location: '', mandatoryOnsite: false, synonymsApplied: [], warnings: [] }
      }
    })
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Analyze JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(analyze).not.toHaveBeenCalled()
  })

  it('shows why analysis failed instead of loading forever', async () => {
    vi.spyOn(mock, 'analyzeDemand').mockRejectedValue(new Error('JD text is blank'))
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Analyze JD' }))[0])
    await screen.findByText(/JD text is blank/)
    expect(screen.queryByRole('button', { name: /Confirm & Start/ })).toBeNull()
  })

  it('reads an existing draft without re-analyzing', async () => {
    const analyze = vi.spyOn(mock, 'analyzeDemand')
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(analyze).not.toHaveBeenCalled()
  })
})
