import { fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, renderApp, seed } from '../../test/utils'
import { api, ApiError } from '../../api'

const locked = (retryAfterSeconds?: number) =>
  new ApiError({ title: 'Locked', status: 423, detail: 'Too many failed sign-ins.', code: 'ACCOUNT_LOCKED', retryAfterSeconds })

async function submit() {
  fireEvent.change(await screen.findByLabelText('Corporate email'), { target: { value: ADMIN.email } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
}

describe('sign-in lockout (REQ-AUTH-04)', () => {
  beforeEach(async () => {
    await seed()
  })
  afterEach(() => vi.restoreAllMocks())

  it('counts down when the 423 carries Retry-After', async () => {
    vi.spyOn(api, 'login').mockRejectedValueOnce(locked(90))
    renderApp('/login')
    await submit()
    expect(await screen.findByText('Too many attempts. Try again in 01:30.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Sign in' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a plain lockout message without an expiry', async () => {
    vi.spyOn(api, 'login').mockRejectedValueOnce(locked())
    renderApp('/login')
    await submit()
    expect(await screen.findByText(/The account is temporarily locked\. Try again later/)).toBeTruthy()
    expect(screen.queryByText(/Try again in/)).toBeNull()
  })
})
