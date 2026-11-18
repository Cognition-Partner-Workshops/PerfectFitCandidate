import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { mock } from '../../test/utils'
import { ADMIN_SETTINGS } from '../../api/mock/fixtures'
import { OccAdminSettings } from './OccAdminSettings'

afterEach(() => vi.restoreAllMocks())

function setup() {
  vi.spyOn(mock, 'getAdminSettings').mockResolvedValue({ ...ADMIN_SETTINGS })
  const save = vi.spyOn(mock, 'saveAdminSettings').mockImplementation(async (settings) => settings)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><OccAdminSettings /></QueryClientProvider>)
  return save
}

it('saves reminder settings alongside the transport', async () => {
  const save = setup()
  const time = await screen.findByLabelText('Reminder time')
  fireEvent.change(time, { target: { value: '10:30' } })
  fireEvent.change(screen.getByLabelText(/Holidays \(ISO/), { target: { value: '2026-12-25, 2026-12-28' } })
  await userEvent.click(screen.getByLabelText('Enable Outlook follow-up reminder'))
  await userEvent.click(screen.getByRole('button', { name: 'Save admin settings' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ followUpEnabled: false, followUpTime: '10:30', followUpHolidays: ['2026-12-25', '2026-12-28'], emailTransport: 'eml_handoff' })))
})

it('validates dates and displays backend validation errors', async () => {
  const save = setup()
  const holidays = await screen.findByLabelText(/Holidays \(ISO/)
  fireEvent.change(holidays, { target: { value: '2026-02-30' } })
  expect(screen.getByText(/Holidays must be valid ISO/)).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Save admin settings' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(holidays, { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Reminder time'), { target: { value: '' } })
  expect(screen.getByText(/Reminder time must be HH:MM/)).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Reminder time'), { target: { value: '09:00' } })
  save.mockRejectedValue(new Error('Follow-up time must be HH:MM (422)'))
  await userEvent.click(screen.getByRole('button', { name: 'Save admin settings' }))
  expect(await screen.findByText('Follow-up time must be HH:MM (422)')).toBeTruthy()
})
