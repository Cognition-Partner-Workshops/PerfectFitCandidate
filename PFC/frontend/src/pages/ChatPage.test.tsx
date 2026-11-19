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

it('renders query rows as masked values with SQL details, truncation and human-review guidance', async () => {
  const sql = "SELECT resource_ref,rank,score FROM chat_v_match_results WHERE requisition='T10001' ORDER BY rank LIMIT 5"
  vi.spyOn(mock, 'chatDetail').mockResolvedValue({
    ...entry,
    answer: {
      outcome: 'answered', text: 'Stored results. Returned 2 rows.', sources: [],
      query: {
        sql, explanation: 'This lists the stored matches.',
        columns: ['resource_ref', 'rank', 'score'],
        rows: [['Resource #42', 1, 84.5], ['[name masked]', 2, null]],
        truncated: true, ranking: true,
      },
    },
  })
  renderApp('/chat?job=37')
  const table = await screen.findByRole('table', { name: 'Query results' })
  expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent))
    .toEqual(['resource_ref', 'rank', 'score'])
  expect(within(table).getByText('Resource #42')).toBeTruthy()
  expect(within(table).getByText('[name masked]')).toBeTruthy()
  expect(within(table).getByText('84.5')).toBeTruthy()
  expect(within(table).getByText('—')).toBeTruthy()
  expect(screen.getByText('Results truncated to 500 rows.')).toBeTruthy()
  expect(screen.getByText('PFC ranking — human review required')).toBeTruthy()
  const toggle = screen.getByText('Show SQL')
  const details = toggle.closest('details')!
  expect(details.open).toBe(false)
  fireEvent.click(toggle)
  expect(details.open).toBe(true)
  expect(within(details).getByText(sql)).toBeTruthy()
  expect(within(details).getByText('This lists the stored matches.')).toBeTruthy()
})

it('shows empty query results without ranking or truncation notices', async () => {
  vi.spyOn(mock, 'chatDetail').mockResolvedValue({
    ...entry,
    answer: {
      outcome: 'no_data', text: 'Returned 0 rows.', sources: [],
      query: { sql: 'SELECT grade FROM chat_v_resources WHERE 0', explanation: 'This lists grades.',
        columns: ['grade'], rows: [], truncated: false, ranking: false },
    },
  })
  renderApp('/chat?job=37')
  expect(await screen.findByText('No result rows.')).toBeTruthy()
  expect(screen.queryByText('PFC ranking — human review required')).toBeNull()
  expect(screen.queryByText(/truncated to 500 rows/)).toBeNull()
})

it('offers statistics and ranking examples and exercises the mock query response', async () => {
  renderApp('/chat')
  await screen.findByText('Local model ready')
  const example = await screen.findByRole('button', { name: 'Top 5 candidates for T10001' })
  expect(screen.getByRole('button', { name: 'Average match score by band for current results' })).toBeTruthy()
  fireEvent.click(example)
  await waitFor(() => expect(input().disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
  expect(await screen.findByRole('table', { name: 'Query results' })).toBeTruthy()
  expect(screen.getByText('PFC ranking — human review required')).toBeTruthy()
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
