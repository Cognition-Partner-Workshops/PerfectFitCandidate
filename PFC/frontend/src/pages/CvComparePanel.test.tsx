import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

/** CV version comparison (REQ-IN-24 / FR-ING-52): select two versions, read-only, masked. */
describe('Candidate Profile CV version comparison', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  const openCvTab = async (id: number) => {
    renderApp(`/candidates/${id}`)
    fireEvent.click(await screen.findByRole('tab', { name: /CV versions/ }, { timeout: 5000 }))
  }

  it('enables Compare only for exactly two versions and shows facts, text diff and stored scores', async () => {
    await openCvTab(1)
    const button = await screen.findByRole('button', { name: /Compare selected \(0\/2\)/ })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByLabelText('Select CV-10 for comparison'))
    fireEvent.click(screen.getByLabelText('Select CV-11 for comparison'))
    const ready = screen.getByRole('button', { name: /Compare selected \(2\/2\)/ })
    expect((ready as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(ready)
    const region = await screen.findByRole('region', { name: 'CV version comparison' })
    expect(within(region).getByText(/Compare CV-11 \(v1\) ↔ CV-10 \(v2, latest\)/)).toBeTruthy()
    expect(within(region).getByText('Read-only')).toBeTruthy()
    const skills = within(region).getByRole('region', { name: 'Skills' })
    expect(within(skills).getAllByText('Agile')[0].closest('tr')!.textContent).toContain('Added')
    expect(within(skills).getAllByText('SQL')[0].closest('tr')!.textContent).toContain('Unchanged')
    const facts = within(region).getByRole('region', { name: 'Facts' })
    expect(within(facts).getAllByText('Years of experience')[0].closest('tr')!.textContent).toContain('Changed')
    const diff = within(region).getByRole('region', { name: 'Text differences' })
    expect(diff.querySelectorAll('td.diff-new').length).toBeGreaterThan(0)
    expect(region.textContent).not.toContain('900189')
    expect(region.textContent).not.toContain('AlexAlexander@')
    expect(within(region).queryByRole('textbox')).toBeNull()
  })

  it('scores both versions as a background job and shows per-demand deltas and breakdowns', async () => {
    const start = vi.spyOn(mock, 'startCvComparison')
    renderApp('/candidates/1?compare=11,10')
    const region = await screen.findByRole('region', { name: 'CV version comparison' }, { timeout: 5000 })
    const scores = within(region).getByRole('region', { name: 'Score differences' })
    fireEvent.click(within(scores).getByRole('button', { name: 'Score both versions' }))
    await waitFor(() => expect(start).toHaveBeenCalledWith(1, 11, 10))
    const table = await within(scores).findByRole('table', { name: 'What-if scores' })
    expect(within(table).getAllByText('+8').length).toBeGreaterThan(0)
    fireEvent.click(within(table).getAllByText('Score breakdown')[0])
    expect(within(table).getAllByText('skills').length).toBeGreaterThan(0)
    expect(within(scores).getByRole('button', { name: 'Re-score both versions' })).toBeTruthy()
  })

  it('closing returns to the CV facts cards and same-version selection is impossible', async () => {
    renderApp('/candidates/1?compare=11,10')
    const region = await screen.findByRole('region', { name: 'CV version comparison' }, { timeout: 5000 })
    fireEvent.click(within(region).getByRole('button', { name: 'Close comparison' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'CV version comparison' })).toBeNull())
    expect(await screen.findByText(/CV facts —/)).toBeTruthy()
    await expect(mock.compareCvVersions(1, 10, 10)).rejects.toThrow()
    await expect(mock.compareCvVersions(1, 10, 51)).rejects.toThrow()
  })
})
