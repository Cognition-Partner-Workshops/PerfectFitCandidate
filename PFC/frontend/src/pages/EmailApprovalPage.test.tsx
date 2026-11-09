import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EmailDraft, EmailTransportStatus } from '../api/types'
import { mock } from '../test/utils'
import { EmailApprovalPage } from './EmailApprovalPage'

const draft = (handoffAt?: string): EmailDraft => ({
  emailId: 1,
  resultId: 2,
  demandId: 3,
  codeRequisition: 'REQ-1',
  accountName: 'Acme',
  demandRequestName: 'Analyst',
  candidateName: 'Alex Candidate',
  rank: 1,
  score: 90,
  band: 'Strong',
  toMasked: 'a***@example.com',
  cc: [],
  subject: 'Opportunity',
  body: '<p>Details</p>',
  status: 'Approved',
  preselected: false,
  demandUploadedAt: '2026-01-01T00:00:00Z',
  handoffAt,
})

function renderPage(email: EmailDraft, transport: EmailTransportStatus) {
  vi.spyOn(mock, 'listEmails').mockResolvedValue({ items: [email], transport })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <EmailApprovalPage />
    </QueryClientProvider>,
  )
}

afterEach(() => vi.restoreAllMocks())

describe('EmailApprovalPage transport handoff', () => {
  it('offers Open in Outlook for draft files', async () => {
    const user = userEvent.setup()
    const fileDraft = draft()
    renderPage(fileDraft, { transport: 'Outlook draft file', outlookDetected: false })
    await user.click(await screen.findByRole('tab', { name: 'Approved (1)' }))
    const open = await screen.findByRole('button', { name: 'Open in Outlook' })
    expect(screen.queryByRole('button', { name: /^Send$/ })).toBeNull()
    vi.spyOn(mock, 'openEmailInOutlook').mockResolvedValue({ draft: fileDraft, opened: true })
    await user.click(open)
    await waitFor(() => expect(mock.openEmailInOutlook).toHaveBeenCalledWith(fileDraft.emailId))
  })

  it('keeps the COM send button unchanged', async () => {
    const user = userEvent.setup()
    renderPage(draft(), { transport: 'Outlook COM', outlookDetected: true })
    await user.click(await screen.findByRole('tab', { name: 'Approved (1)' }))
    expect(await screen.findByRole('button', { name: /^Send$/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Open in Outlook' })).toBeNull()
  })

  it('shows handoff confirmation actions and marks sent through the API client', async () => {
    const user = userEvent.setup()
    const handedOff = draft('2026-01-02T03:04:05Z')
    renderPage(handedOff, { transport: 'Outlook draft file', outlookDetected: false })
    vi.spyOn(mock, 'confirmEmailHandoff').mockResolvedValue({ ...handedOff, status: 'Sent' })
    await user.click(await screen.findByRole('tab', { name: 'Approved (1)' }))

    expect(
      await screen.findByText(/Check the draft, click Send in Outlook, then confirm here\./),
    ).toBeTruthy()
    expect(screen.getByText('Awaiting confirmation')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Not sent' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open again' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Mark as sent' }))
    await waitFor(() =>
      expect(mock.confirmEmailHandoff).toHaveBeenCalledWith(handedOff.emailId, true),
    )
  })
})
