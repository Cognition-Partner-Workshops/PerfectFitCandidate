import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ADMIN, mock, seed, signIn } from '../../test/utils'
import { MODELS } from '../../api/mock/fixtures'
import { OccModels } from './OccModels'

beforeEach(async () => { await seed(); await signIn('Admin', ADMIN.email, ADMIN.password) })
afterEach(() => vi.restoreAllMocks())

function setup() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><OccModels /></QueryClientProvider>)
}

it('marks Thinking models and saves context with calculated RAM options', async () => {
  const save = vi.spyOn(mock.maintenance, 'saveSettings')
  setup()
  expect(await screen.findByText('Thinking')).toBeTruthy()
  expect(screen.getByText('slower: reasons before answering (~4× wall time on JD parsing)')).toBeTruthy()
  const select = await screen.findByLabelText('Context size (tokens)') as HTMLSelectElement
  expect(select.value).toBe('4096')
  expect(Array.from(select.options).map((option) => option.textContent)).toEqual([
    '4096 (default) · extra RAM: 0 MiB',
    '8192 · extra RAM: 576 MiB',
    '16384 · extra RAM: 1.69 GiB (1728 MiB)',
  ])
  expect(screen.getByText(/Saving restarts the local model/)).toBeTruthy()
  fireEvent.change(select, { target: { value: '8192' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save context size' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith({ 'llm.ctx_size': 8192 }))
  expect(await screen.findByText(/Context size saved/)).toBeTruthy()
  expect((await mock.maintenance.settings())['llm.ctx_size']).toBe(8192)
})

it('loads the saved setting and shows backend validation errors', async () => {
  await mock.maintenance.saveSettings({ 'llm.ctx_size': 8192 })
  vi.spyOn(mock.maintenance, 'saveSettings').mockRejectedValue(new Error('llm.ctx_size must be 4096, 8192 or 16384 (422)'))
  setup()
  const select = await screen.findByLabelText('Context size (tokens)') as HTMLSelectElement
  expect(select.value).toBe('8192')
  fireEvent.change(select, { target: { value: '16384' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save context size' }))
  expect(await screen.findByText('llm.ctx_size must be 4096, 8192 or 16384 (422)')).toBeTruthy()
  expect(select.value).toBe('16384')
})

it('shows unknown RAM without KV metadata or a default generation model', async () => {
  vi.spyOn(mock, 'listOccModels').mockResolvedValue(MODELS.map((model) => ({ ...model, kvBytesPerToken: null })))
  setup()
  const select = await screen.findByLabelText('Context size (tokens)') as HTMLSelectElement
  expect(Array.from(select.options).every((option) => option.textContent?.includes('unknown'))).toBe(true)
})

it('handles an empty model list and settings fetch failure', async () => {
  vi.spyOn(mock, 'listOccModels').mockResolvedValue([])
  vi.spyOn(mock.maintenance, 'settings').mockRejectedValue(new Error('Settings unavailable'))
  setup()
  expect(await screen.findByText('Settings unavailable')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Save context size' })).toBeNull()
})

it('rejects invalid context values in the mock', async () => {
  await expect(mock.maintenance.saveSettings({ 'llm.ctx_size': '8192' })).rejects.toMatchObject({ status: 422 })
})
