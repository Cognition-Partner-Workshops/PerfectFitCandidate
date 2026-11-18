import { fireEvent, screen, within, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'
import { toDashboardQuery, toResultsReportQuery } from '../api/mappers'
import { formatMetric, formatPct, formatServicePct } from '../lib/format'

const filters = { codeRequisition: ['T10001'], accountName: ['Harbour Retail'], demandGrade: ['C1'], personGrade: ['C2'], location: ['Manchester'], scoreMin: 61, scoreMax: 99, candidateType: ['Internal'] }

beforeEach(async () => { await seed(); await signIn('Admin', ADMIN.email, ADMIN.password) })
afterEach(() => vi.restoreAllMocks())

describe('PDF report dialog', () => {
  it.each(['/dashboard', '/matches'])('uses the exact page filter mapping on %s', async (path) => {
    sessionStorage.setItem('pfc-result-filters', JSON.stringify(filters))
    const submit = vi.spyOn(mock, 'generatePdf').mockResolvedValue({ jobId: 90 })
    renderApp(path)
    fireEvent.click(await screen.findByRole('button', { name: 'PDF report' }))
    const dialog = screen.getByRole('dialog', { name: 'PDF report' })
    expect(within(dialog).getByText(/Emails are always masked/)).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(submit).toHaveBeenCalledOnce())
    const payload = submit.mock.calls[0]![0]
    expect(payload.scope).toBe('filters')
    if (path === '/dashboard') {
      expect(payload.filters).toEqual(toDashboardQuery(filters))
      expect(payload.orientation).toBe('portrait')
      expect(payload.sections).toHaveLength(8)
    } else {
      expect(payload.resultFilters).toEqual(toResultsReportQuery(filters, true))
      expect(payload.orientation).toBe('landscape')
      expect(payload.sections).toEqual(['cover', 'match-results'])
    }
  })

  it('scope all omits filters; empty sections block submission', async () => {
    const submit = vi.spyOn(mock, 'generatePdf').mockResolvedValue({ jobId: 91 })
    renderApp('/dashboard')
    fireEvent.click(await screen.findByRole('button', { name: 'PDF report' }))
    const dialog = screen.getByRole('dialog')
    for (const checkbox of within(dialog).getAllByRole('checkbox')) fireEvent.click(checkbox)
    expect((within(dialog).getByRole('button', { name: 'Generate' }) as HTMLButtonElement).disabled).toBe(true)
    expect(submit).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByLabelText('Summary and KPIs'))
    fireEvent.change(within(dialog).getByLabelText('Scope'), { target: { value: 'all' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ report: 'dashboard', scope: 'all', sections: ['summary'], orientation: 'portrait' }))
  })

  it('requires both results sections and preserves errors for retry', async () => {
    vi.spyOn(mock, 'generatePdf').mockRejectedValue(new Error('Unavailable'))
    renderApp('/matches')
    fireEvent.click(await screen.findByRole('button', { name: 'PDF report' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByLabelText('Cover and provenance'))
    expect((within(dialog).getByRole('button', { name: 'Generate' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(within(dialog).getByLabelText('Cover and provenance'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Generate' }))
    expect(await within(dialog).findByRole('alert')).toBeTruthy()
  })

  it('uses score descending for the flat grid and omits filters for results scope all', async () => {
    const submit = vi.spyOn(mock, 'generatePdf').mockResolvedValue({ jobId: 92 })
    renderApp('/matches')
    fireEvent.click(await screen.findByLabelText('Flat list'))
    fireEvent.click(screen.getByRole('button', { name: 'PDF report' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(submit).toHaveBeenCalledOnce())
    expect(submit.mock.calls[0]![0].resultFilters).toMatchObject({ sort: 'score', descending: true })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'PDF report' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Scope'), { target: { value: 'all' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2))
    expect(submit.mock.calls[1]![0]).toEqual({ report: 'match_results', scope: 'all', orientation: 'landscape', sections: ['cover', 'match-results'] })
  })
})

describe('PDF formatter parity', () => {
  it('matches builder.match_pct and preserves service precision in values tables', () => {
    for (const [input, expected] of [[0, '0 %'], [60.49, '60 %'], [60.5, '61 %'], [90.5, '91 %'], [100, '100 %']] as const) expect(formatPct(input)).toBe(expected)
    expect(formatMetric(12.25)).toBe('12.25')
    expect(formatMetric(10.0)).toBe('10')
    expect(formatMetric(null)).toBe('Not recorded')
    expect(formatServicePct(66.67)).toBe('66.67 %')
    expect(formatMetric(0.1234)).toBe('0.1234')
    expect(toResultsReportQuery(filters, false)).toMatchObject({ sort: 'score', descending: true })
  })
})
