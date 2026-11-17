import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api'
import { ADMIN, ANALYST_PASSWORD, renderApp, seed, signIn } from '../../test/utils'

describe('CV experience and degree rule switch', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })
  afterEach(() => vi.restoreAllMocks())

  it('defaults on for older templates and preserves off when publishing', async () => {
    const templates = await api.listTemplates()
    vi.spyOn(api, 'listTemplates').mockResolvedValueOnce(templates.map((t) => ({ ...t, cvExperienceDegree: undefined })))
    renderApp('/occ/scoring-template')
    const toggle = await screen.findByRole('checkbox', { name: 'Use CV experience & degree rules' }) as HTMLInputElement
    expect(toggle.checked).toBe(true)
    fireEvent.click(toggle)
    fireEvent.change(screen.getByLabelText('Change note (required)'), { target: { value: 'Use legacy qualification scoring' } })
    fireEvent.click(screen.getByRole('button', { name: /Publish v4/ }))
    await waitFor(async () => expect((await api.listTemplates()).find((t) => t.status === 'active')?.cvExperienceDegree).toBe(false))
    await waitFor(() => expect(screen.getByText(/Active: v4/)).toBeTruthy())
    expect((screen.getByRole('checkbox', { name: 'Use CV experience & degree rules' }) as HTMLInputElement).checked).toBe(false)
  })

  it('discards a switch edit and preserves it after a publish error', async () => {
    renderApp('/occ/scoring-template')
    const toggle = await screen.findByRole('checkbox', { name: 'Use CV experience & degree rules' }) as HTMLInputElement
    fireEvent.click(toggle)
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
    expect(toggle.checked).toBe(true)
    fireEvent.click(toggle)
    fireEvent.change(screen.getByLabelText('Change note (required)'), { target: { value: 'Disable rules' } })
    vi.spyOn(api, 'publishTemplate').mockRejectedValueOnce(new Error('Cannot publish'))
    fireEvent.click(screen.getByRole('button', { name: /Publish v4/ }))
    expect(await screen.findByText('Cannot publish')).toBeTruthy()
    expect(toggle.checked).toBe(false)
    expect((await api.listTemplates()).find((t) => t.status === 'active')?.cvExperienceDegree).toBe(true)
  })

  it('does not allow Analysts to change OCC rules', async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
    renderApp('/occ/scoring-template')
    expect(await screen.findByText("You don't have access to this area")).toBeTruthy()
    expect(screen.queryByRole('checkbox', { name: 'Use CV experience & degree rules' })).toBeNull()
  })
})
