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

  it('analyzes a new demand before reading its requirements', async () => {
    const calls: string[] = []
    const analyze = vi.spyOn(mock, 'analyzeDemands')
    const get = vi.spyOn(mock, 'getRequirements')
    analyze.mockImplementation(async () => void calls.push('analyze'))
    get.mockImplementation(async (id) => {
      calls.push('get')
      get.mockRestore()
      return mock.getRequirements(id)
    })
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Analyze JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(calls).toEqual(['analyze', 'get'])
    expect(analyze).toHaveBeenCalledTimes(1)
  })

  it('reads an existing draft without re-analyzing', async () => {
    const analyze = vi.spyOn(mock, 'analyzeDemands')
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
    expect(analyze).not.toHaveBeenCalled()
  })
})
