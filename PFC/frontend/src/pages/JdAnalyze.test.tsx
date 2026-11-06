import { fireEvent, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

async function openRow(code: string, button: 'Analyze JD' | 'Review JD') {
  renderApp('/demands')
  fireEvent.click(within(await screen.findByRole('row', { name: new RegExp(code) })).getByRole('button', { name: button }))
}

/** The JD review drawer analyzes only when the backend JD status says the current JD has no requirements (REQ-MAT-02). */
describe('Analyze JD', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  it('analyzes a Not analysed demand once and shows the draft it returns', async () => {
    const analyze = vi.spyOn(mock, 'analyzeDemand')
    await openRow('ABCD123', 'Analyze JD')
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(analyze).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/JD has changed since the last analysis/)).toBeNull()
  })

  it('re-analyzes a JD changed demand and says the JD changed', async () => {
    const analyze = vi.spyOn(mock, 'analyzeDemand')
    await openRow('T1J550', 'Analyze JD')
    await screen.findByText(/JD has changed since the last analysis/)
    expect(analyze).toHaveBeenCalledTimes(1)
  })

  it('reads a Draft without re-analyzing', async () => {
    const analyze = vi.spyOn(mock, 'analyzeDemand')
    await openRow('T1G101', 'Review JD')
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(analyze).not.toHaveBeenCalled()
  })

  it('falls back to analysis when the status is stale and no requirements exist', async () => {
    vi.spyOn(mock, 'getRequirements').mockRejectedValueOnce(new ApiError({ title: 'Requirements not found', status: 404, code: 'REQUIREMENTS_NOT_FOUND' }))
    const analyze = vi.spyOn(mock, 'analyzeDemand')
    await openRow('T1G101', 'Review JD')
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(analyze).toHaveBeenCalledTimes(1)
  })

  it('shows why analysis failed instead of loading forever', async () => {
    vi.spyOn(mock, 'analyzeDemand').mockRejectedValue(new Error('JD text is blank'))
    await openRow('ABCD123', 'Analyze JD')
    await screen.findByText(/JD text is blank/)
    expect(screen.queryByRole('button', { name: /Confirm & Start/ })).toBeNull()
  })
})
