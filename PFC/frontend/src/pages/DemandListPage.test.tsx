import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { JdRequirements } from '../api/types'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

/** JD review shows how requirements were extracted (REQ-MAT-02); Confirm & Start stays a manual step. */
describe('JD review extraction method', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  const open = async (req: Partial<JdRequirements>) => {
    const base = await mock.getRequirements(1)
    vi.spyOn(mock, 'getRequirements').mockResolvedValue({ ...base, ...req })
    const confirm = vi.spyOn(mock, 'confirmRequirements')
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
    return confirm
  }

  it('warns with the backend reason on a rule-based fallback and hides the duplicate warning', async () => {
    const confirm = await open({
      extractionMethod: 'rule_based_fallback',
      fallbackReason: 'schema_invalid',
      warnings: ['Rule-based fallback: schema_invalid'],
    })
    expect(screen.getByText('Extracted by rules, not the local AI model')).toBeTruthy()
    expect(screen.getByText(/reason: schema_invalid/)).toBeTruthy()
    expect(screen.getByText(/Check every requirement carefully before Confirm & Start/)).toBeTruthy()
    expect(screen.queryByText('Rule-based fallback: schema_invalid')).toBeNull()
    expect(screen.queryByText('Extracted by local AI model')).toBeNull()
    expect(confirm).not.toHaveBeenCalled()
  })

  it('shows a quiet note when the local model extracted the requirements', async () => {
    await open({ extractionMethod: 'llm', fallbackReason: null })
    expect(screen.getByText('Extracted by local AI model')).toBeTruthy()
    expect(screen.queryByText('Extracted by rules, not the local AI model')).toBeNull()
  })

  it('shows nothing extra for older rows without an extraction method', async () => {
    await open({ extractionMethod: null, fallbackReason: null })
    expect(screen.queryByText('Extracted by local AI model')).toBeNull()
    expect(screen.queryByText('Extracted by rules, not the local AI model')).toBeNull()
  })

  it('mock demand 2 falls back to rules with a reason code', async () => {
    expect(await mock.getRequirements(2)).toMatchObject({
      extractionMethod: 'rule_based_fallback',
      fallbackReason: 'provider_unavailable',
    })
  })
})

describe('JD review source text', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  it('renders the JD text fetched from demand detail', async () => {
    const jdText = 'JD text from the current demand version'
    const getDemand = vi.spyOn(mock, 'getDemand').mockResolvedValue({
      ...(await mock.getDemand(1)),
      jdText,
    })
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])

    expect(await screen.findByText(jdText)).toBeTruthy()
    expect(getDemand).toHaveBeenCalledWith(1)
  })

  it('shows a muted message when the uploaded DL has no JD text', async () => {
    vi.spyOn(mock, 'getDemand').mockResolvedValue({ ...(await mock.getDemand(1)), jdText: '' })
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])

    expect(await screen.findByText('No JD text in the uploaded DL')).toBeTruthy()
  })
})

/** JD review edits qualifications and the location/onsite flag extracted from the JD, apart from the DL region (REQ-MAT-02). */
describe('JD review qualifications and location', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  const open = async (req: Partial<JdRequirements>) => {
    const base = await mock.getRequirements(1)
    vi.spyOn(mock, 'getRequirements').mockResolvedValue({ ...base, ...req })
    renderApp('/demands')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Review JD' }))[0])
    await screen.findByRole('button', { name: /Confirm & Start/ })
  }
  const input = (label: string | RegExp) => screen.getByLabelText(label) as HTMLInputElement

  it('shows the extracted values and labels the DL region separately', async () => {
    await open({ qualifications: 'BSc Computing', location: 'Leeds', mandatoryOnsite: true })
    expect(screen.getByText(/DL region:/)).toBeTruthy()
    expect(input(/Qualifications/).value).toBe('BSc Computing')
    expect(input('Location (extracted from JD)').value).toBe('Leeds')
    expect(input(/Mandatory\s+onsite/).checked).toBe(true)
  })

  it('saves edited qualifications, location and onsite through the requirements PUT', async () => {
    await open({ qualifications: '', location: '', mandatoryOnsite: false })
    const save = vi.spyOn(mock, 'saveRequirements')
    fireEvent.change(input(/Qualifications/), { target: { value: 'BSc; PRINCE2' } })
    fireEvent.change(input('Location (extracted from JD)'), { target: { value: 'Glasgow' } })
    fireEvent.click(input(/Mandatory\s+onsite/))
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0]?.[0]).toMatchObject({ qualifications: 'BSc; PRINCE2', location: 'Glasgow', mandatoryOnsite: true })
  })
})
