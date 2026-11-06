import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api'
import type { DocumentSummary } from '../api/types'
import { SummaryCard } from './SummaryCard'

const summary: DocumentSummary = {
  sentences: [{ text: 'SQL experience summary', evidence: 'SQL engineer' }],
  model: 'local-test', createdAt: '2026-11-06T12:00:00Z', stale: false,
  droppedUngrounded: 0, sourceVersionId: 1,
}
const missing = () => new ApiError({ status: 404, code: 'SUMMARY_NOT_FOUND', title: 'Not found' })
function open(kind: 'candidate' | 'demand' = 'candidate') {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <SummaryCard kind={kind} id={1} />
  </QueryClientProvider>)
}
afterEach(() => { cleanup(); vi.restoreAllMocks() })

async function clickGenerate() {
  const button = await screen.findByRole('button', { name: 'Generate summary' }) as HTMLButtonElement
  await waitFor(() => expect(button.disabled).toBe(false))
  fireEvent.click(button)
}

describe('reading-only summaries', () => {
  it.each(['candidate', 'demand'] as const)('generates a %s summary, with pending state and expandable evidence', async (kind) => {
    vi.spyOn(api, kind === 'candidate' ? 'getCandidateSummary' : 'getDemandSummary').mockRejectedValue(missing())
    let resolve!: (value: DocumentSummary) => void
    const generate = vi.spyOn(api, kind === 'candidate' ? 'generateCandidateSummary' : 'generateDemandSummary')
      .mockReturnValue(new Promise<DocumentSummary>((done) => { resolve = done }))
    open(kind)
    expect(screen.getByText('AI summary for reading only — not used in scoring. Check against the source.')).toBeTruthy()
    await clickGenerate()
    expect((await screen.findByRole('button', { name: 'Generating…' }) as HTMLButtonElement).disabled).toBe(true)
    await act(async () => resolve(summary))
    expect(await screen.findByText('SQL experience summary')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Regenerate' })).toBeTruthy()
    const evidence = screen.getByText('Evidence')
    fireEvent.click(evidence)
    expect(evidence.closest('details')?.open).toBe(true)
    expect(screen.getByText('SQL engineer')).toBeTruthy()
    expect(generate).toHaveBeenCalledWith(1)
  })

  it('loads an existing stale summary and clears the warning after regeneration', async () => {
    vi.spyOn(api, 'getCandidateSummary').mockResolvedValue({ ...summary, stale: true })
    vi.spyOn(api, 'generateCandidateSummary').mockResolvedValue(summary)
    open()
    expect(await screen.findByText('Source changed since this summary — regenerate')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }))
    await waitFor(() => expect(screen.queryByText('Source changed since this summary — regenerate')).toBeNull())
  })

  it.each([
    [503, 'LLM_UNAVAILABLE', 'The local AI is unavailable. Try again when it is ready.'],
    [422, 'SUMMARY_INVALID', 'The AI did not return a valid, source-supported summary. Try again.'],
    [422, 'SUMMARY_UNGROUNDED', 'The AI did not return a valid, source-supported summary. Try again.'],
    [409, 'SUMMARY_NO_SOURCE', 'No source document is recorded. Add a CV or job description first.'],
  ] as const)('maps %s %s to a plain message', async (status, code, message) => {
    vi.spyOn(api, 'getCandidateSummary').mockRejectedValue(missing())
    vi.spyOn(api, 'generateCandidateSummary').mockRejectedValue(new ApiError({ status, code, title: 'Technical failure' }))
    open()
    await clickGenerate()
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', message)
    expect(screen.getByRole('button', { name: 'Generate summary' })).toBeTruthy()
  })
})
