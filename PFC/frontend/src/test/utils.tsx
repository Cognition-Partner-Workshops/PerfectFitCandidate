import { render } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../api'
import { MockApiClient } from '../api/mock/mockClient'
import { AppRoutes } from '../App'
import { createQueryClient } from '../lib/queryClient'

export const mock = api as MockApiClient

export const ADMIN = { fullName: 'Olivia Admin', email: 'olivia.admin@example.com', password: 'Str0ng!Passphrase#42' }
export const ANALYST_PASSWORD = 'Bench#Matching2026'

/** Fresh mock state with one Admin (and optionally one Analyst whose password is already changed). */
export async function seed(options: { analyst?: boolean } = {}): Promise<{ analystEmail: string }> {
  localStorage.clear()
  mock.reset()
  await mock.setupAdmin({ setupCode: mock.setupCode, ...ADMIN })
  const analystEmail = 'bea.analyst@example.com'
  if (options.analyst) {
    await mock.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    const otp = await mock.createUser({ fullName: 'Bea Analyst', email: analystEmail, role: 'Analyst', persona: 'A' })
    await mock.logout()
    await mock.login({ role: 'Analyst', email: analystEmail, password: otp.oneTimePassword })
    await mock.changePassword({ currentPassword: otp.oneTimePassword, newPassword: ANALYST_PASSWORD })
  }
  await mock.logout()
  return { analystEmail }
}

export async function signIn(role: 'Admin' | 'Analyst', email: string, password: string): Promise<void> {
  await mock.login({ role, email, password })
}

export function renderApp(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
