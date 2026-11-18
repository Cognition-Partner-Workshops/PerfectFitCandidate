import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ChatEntry } from '../api/types'
import { ADMIN, mock, renderApp, seed, signIn } from '../test/utils'

const question = 'How many open demands are there?'
const input = () => screen.getByRole('textbox', { name: 'Question' }) as HTMLTextAreaElement
const entry: ChatEntry = {
  jobId: 37, question, status: 'completed', createdAt: '2026-11-06T12:00:00Z',
}

beforeEach(async () => {
  await seed()
  await signIn('Admin', ADMIN.email, ADMIN.password)
})
afterEach(() => vi.restoreAllMocks())

it('submits an example and shows the backend answer, linked sources and private history', async () => {
  const ask = vi.spyOn(mock, 'askChat').mockResolvedValue({ ...entry, status: 'queued' })
  vi.spyOn(mock, 'chatStatus').mockResolvedValue({ available: true })
  vi.spyOn(mock, 'chatHistory').mockResolvedValue([entry])
  vi.spyOn(mock, 'chatDetail').mockResolvedValue({
    ...entry,
    answer: {
      outcome: 'answered', text: 'Count of open demands: 1.',
      sources: [{
        kind: 'demand', recordId: 1, label: 'T10001', link: '/demands?demand=1',
        values: { 'Start date': '2026-11-09' },
      }],
    },
  })

  renderApp('/chat')
  await screen.findByText('Local model ready')
  fireEvent.click(within(screen.getByRole('region', { name: 'Ask PFC' }))
    .getByRole('button', { name: question }))
  expect(input().value).toBe(question)
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
  expect(await screen.findByText('Count of open demands: 1.')).toBeTruthy()
  expect(ask).toHaveBeenCalledWith(question, expect.anything())
  const sources = within(screen.getByRole('region', { name: 'Sources (1)' }))
  expect(sources.getByRole('link', { name: 'T10001' }).getAttribute('href')).toBe('/demands?demand=1')
  expect(sources.getByText('2026-11-09')).toBeTruthy()
  const history = within(screen.getByRole('complementary', { name: 'Question history' }))
  expect(history.getByRole('button', { name: /How many open demands/ }).getAttribute('aria-current')).toBe('true')
})

it('allows stopping a running question and renders the cancelled state', async () => {
  vi.spyOn(mock, 'chatStatus').mockResolvedValue({ available: true })
  const detail = vi.spyOn(mock, 'chatDetail').mockResolvedValue({ ...entry, status: 'running', answer: null })
  const cancel = vi.spyOn(mock, 'cancelJob').mockImplementation(async () => {
    detail.mockResolvedValue({ ...entry, status: 'cancelled', answer: null })
  })
  renderApp('/chat?job=37')
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
  expect(await screen.findByText('Question stopped.')).toBeTruthy()
  expect(cancel).toHaveBeenCalledWith(37, expect.anything())
  expect(screen.queryByRole('region', { name: /Sources/ })).toBeNull()
})

it('makes model outages visible while keeping saved no-data answers readable', async () => {
  const status = vi.spyOn(mock, 'chatStatus').mockResolvedValue({ available: false })
  vi.spyOn(mock, 'chatDetail').mockResolvedValue({
    ...entry, answer: { outcome: 'no_data', text: "I couldn't find that in PFC data.", sources: [] },
  })
  renderApp('/chat?job=37')
  expect(await screen.findByText("I couldn't find that in PFC data.")).toBeTruthy()
  expect(screen.getByText('No source rows.')).toBeTruthy()
  expect(input().disabled).toBe(true)
  status.mockResolvedValue({ available: true })
  fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
  await waitFor(() => expect(input().disabled).toBe(false))
})
