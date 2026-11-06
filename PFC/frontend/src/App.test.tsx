import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, ANALYST_PASSWORD, mock, renderApp, seed, signIn } from './test/utils'

vi.mock('./components/EChart', () => ({
  EChart: ({ ariaLabel }: { ariaLabel: string }) => <div role="img" aria-label={ariaLabel} />,
}))

describe('routing guards (GUI_OCC_DESIGN.md §7.4, §4.3)', () => {
  beforeEach(() => {
    localStorage.clear()
    mock.reset()
  })

  it('shows first-run setup when no admin exists', async () => {
    renderApp('/dashboard')
    expect(await screen.findByRole('heading', { name: /first-run setup/i })).toBeTruthy()
  })

  it('redirects to login when signed out', async () => {
    await seed()
    renderApp('/matches')
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeTruthy()
  })

  it('signs in an Admin with the role dropdown and shows the OCC tab', async () => {
    await seed()
    renderApp('/login')
    const form = await screen.findByRole('form', { name: 'Sign in' })
    const submit = within(form).getByRole('button', { name: 'Sign in' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(within(form).getByLabelText('Role'), { target: { value: 'Admin' } })
    fireEvent.change(within(form).getByLabelText('Corporate email'), { target: { value: ADMIN.email } })
    fireEvent.change(within(form).getByLabelText('Password'), { target: { value: ADMIN.password } })
    fireEvent.click(submit)
    const nav = await screen.findByRole('navigation', { name: 'Main' })
    expect(within(nav).getByText('OCC')).toBeTruthy()
    expect(within(nav).queryByText('Chat')).toBeNull()
    expect(screen.getByTestId('role-label').textContent).toBe('Admin')
  })

  it('shows a generic error for a role mismatch', async () => {
    await seed()
    renderApp('/login')
    const form = await screen.findByRole('form', { name: 'Sign in' })
    fireEvent.change(within(form).getByLabelText('Role'), { target: { value: 'Analyst' } })
    fireEvent.change(within(form).getByLabelText('Corporate email'), { target: { value: ADMIN.email } })
    fireEvent.change(within(form).getByLabelText('Password'), { target: { value: ADMIN.password } })
    fireEvent.click(within(form).getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('Email, password or role is incorrect.')).toBeTruthy()
  })

  it('hides OCC from Analysts and blocks #/occ deep links', async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
    renderApp('/occ/users')
    expect(await screen.findByText("You don't have access to this area")).toBeTruthy()
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).queryByText('OCC')).toBeNull()
  })

  it('forces a password change after an Admin reset', async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
    const otp = await mock.createUser({ fullName: 'New Analyst', email: 'new@example.com', role: 'Analyst' })
    await mock.logout()
    await signIn('Analyst', 'new@example.com', otp.oneTimePassword)
    renderApp('/dashboard')
    expect(await screen.findByText('Your password was reset. Set a new password to continue.')).toBeTruthy()
  })
})

describe('pages', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })

  it('renders the dashboard KPI header and required charts (REQ-OUT-04..07)', async () => {
    renderApp('/dashboard')
    expect(await screen.findByText('Requisitions assessed')).toBeTruthy()
    expect(screen.getByText('Roles with no suitable candidate')).toBeTruthy()
    expect(await screen.findByRole('img', { name: 'Grade to JD count' })).toBeTruthy()
    expect(screen.getByRole('img', { name: 'Top 5 demanded skills versus eligible bench supply' })).toBeTruthy()
    expect(screen.getByRole('img', { name: 'Bench status distribution' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Demand List rows' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Candidate Tracker rows' })).toBeTruthy()
  })

  it('shows Match Results columns in REQ-OUT-01 order with masked email after the name', async () => {
    renderApp('/matches')
    const table = await screen.findByRole('table', { name: 'Match results' })
    const headers = within(table).getAllByRole('columnheader').map((h) => h.textContent)
    const i = headers.indexOf('Potential Person')
    expect(headers[i + 1]).toBe('Person Email')
    expect(headers.slice(2, 18)[0]).toBe('Requisition')
    expect(within(table).getAllByText(/\*{6}/).length).toBeGreaterThan(0)
    fireEvent.change(screen.getByLabelText('Code Requisition'), { target: { value: 'T2B456' } })
    await waitFor(() => expect(within(table).queryByText('T1A123')).toBeNull())
  })

  it('pre-selects the top 4 of 5 and approves per profile (REQ-EML-01)', async () => {
    renderApp('/email')
    const queue = await screen.findByRole('region', { name: 'Approval queue' })
    const boxes = within(queue).getAllByRole('checkbox', { name: /\(T1A123\)/ }) as HTMLInputElement[]
    expect(boxes.map((b) => b.checked)).toEqual([true, true, true, true, false])
    const editor = screen.getByRole('region', { name: 'Draft editor' })
    const send = within(editor).getByRole('button', { name: /Send/ }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    fireEvent.click(within(editor).getByRole('button', { name: /Approve email to/ }))
    await waitFor(() => expect(within(editor).getByText('Status: Approved')).toBeTruthy())
  })

  it('imports a settings package with preview and rolls back (REQ-OCC-02)', async () => {
    renderApp('/settings/package')
    const input = await screen.findByLabelText('Settings package file')
    fireEvent.change(input, { target: { files: [new File(['x'], 'PFC-settings-v7.pfcpkg')] } })
    expect(await screen.findByText('✓ Signature valid')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Apply package' }))
    expect(await screen.findByText(/Settings package v7 applied/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Roll back' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Roll back to v6' }))
    expect(await screen.findByText('Rolled back to v6.')).toBeTruthy()
  })

  it('rejects a tampered settings package without applying it', async () => {
    renderApp('/settings/package')
    const input = await screen.findByLabelText('Settings package file')
    fireEvent.change(input, { target: { files: [new File(['x'], 'PFC-settings-v8-tampered.pfcpkg')] } })
    expect(await screen.findByText('✕ Signature invalid')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Apply package' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('validates scoring weights total 100 before publishing (REQ-SCR-01)', async () => {
    renderApp('/occ/scoring-template')
    const skills = await screen.findByLabelText('Skills, experience and domain weight')
    fireEvent.change(skills, { target: { value: '60' } })
    expect(screen.getByTestId('weight-total').textContent).toContain('90')
    expect((screen.getByRole('button', { name: /Publish v4/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('creates a user with a one-time password shown once (§16.2)', async () => {
    renderApp('/occ/users')
    fireEvent.click(await screen.findByRole('button', { name: '+ Add user' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Cara Analyst' } })
    fireEvent.change(screen.getByLabelText('Corporate email'), { target: { value: 'cara@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: /Create and generate/ }))
    const otp = await screen.findByTestId('otp')
    expect(otp.textContent?.length).toBeGreaterThanOrEqual(14)
  })

  it('enables Sync selected only when a candidate is selected (REQ-IN-21)', async () => {
    renderApp('/bench')
    const btn = (await screen.findByRole('button', { name: /Sync selected \(0\)/ })) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select ALEXANDER' }))
    expect((screen.getByRole('button', { name: /Sync selected \(1\)/ }) as HTMLButtonElement).disabled).toBe(false)
  })
})
