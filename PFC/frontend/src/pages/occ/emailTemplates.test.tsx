import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, ANALYST_PASSWORD, renderApp, seed, signIn } from '../../test/utils'
import { api } from '../../api'
import { checkTemplate } from '../../lib/occ'

describe('email template placeholders (REQ-EML-03)', () => {
  it('flags tokens outside the allowlist and braces the renderer cannot format', () => {
    const ok = { unknown: [], malformed: false }
    expect(checkTemplate('Opportunity {role}', '<p>{candidate_name} {spoc_1}</p> {{literal}}')).toEqual(ok)
    expect(checkTemplate('{Unknown}', '{other} {Unknown} {}')).toEqual({ unknown: ['Unknown', 'other', ''], malformed: false })
    for (const bad of ['Hi {candidate_name', 'a } b', '<style>a{color:red}</style>', '{role!r}', '{a{b}}'])
      expect(checkTemplate('', bad).malformed).toBe(true)
  })
})

describe('OCC email templates — mock API', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })

  it('appends versions, keeps one default and audits changes', async () => {
    await expect(api.createEmailTemplate({ name: 'candidate_default', subject: '{nope}', bodyHtml: '' })).rejects.toMatchObject({ problem: { code: 'UNKNOWN_PLACEHOLDER' } })
    const v2 = await api.createEmailTemplate({ name: 'candidate_default', subject: 'New {role}', bodyHtml: '<p>{candidate_name}</p>' })
    expect(v2).toMatchObject({ version: 2, isDefault: false })
    expect((await api.listEmailTemplates(false)).map((t) => t.version)).toEqual([2])
    expect((await api.listEmailTemplates(true)).map((t) => t.version)).toEqual([2, 1])
    await api.setDefaultEmailTemplate(v2.emailTemplateId)
    expect((await api.listEmailTemplates(true)).filter((t) => t.isDefault).map((t) => t.version)).toEqual([2])
    expect((await api.getEmailTemplate(v2.emailTemplateId)).subject).toBe('New {role}')
    await expect(api.getEmailTemplate(99999)).rejects.toMatchObject({ status: 404 })
    const log = JSON.stringify(await api.auditLog({}))
    expect(log.includes('Email template candidate_default v2 set as default')).toBe(true)
  })

  it('denies Analysts (REQ-ROLE-03)', async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
    await expect(api.listEmailTemplates(false)).rejects.toMatchObject({ status: 403 })
  })
})

describe('OCC email templates — screen', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })

  it('saves a new version from an edit and sets it as default', async () => {
    renderApp('/occ/email-templates')
    fireEvent.click(await screen.findByRole('button', { name: 'Edit candidate_default v1' }))
    const subject = (await screen.findByLabelText('Subject')) as HTMLInputElement
    expect(subject.value).toBe('Opportunity {requisition} – {role}')
    fireEvent.change(subject, { target: { value: 'Role {bogus}' } })
    expect(screen.getByText('Unknown placeholder: {bogus}')).toBeTruthy()
    const save = screen.getByRole('button', { name: 'Save as v2' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(subject, { target: { value: 'Role {role}' } })
    fireEvent.click(save)
    expect(await screen.findByText(/candidate_default v2 saved/)).toBeTruthy()
    fireEvent.click(await screen.findByRole('button', { name: 'Set candidate_default v2 as default' }))
    expect(await screen.findByText('candidate_default v2 is now the default template.')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Show all versions'))
    const table = await screen.findByRole('table', { name: 'Email template versions' })
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(3))
    expect((within(table).getByRole('button', { name: 'Set candidate_default v1 as default' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('makes the first template the default when none exists', async () => {
    ;(api as unknown as { state: { emailTemplates: unknown[] } }).state.emailTemplates = []
    renderApp('/occ/email-templates')
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    fireEvent.change(screen.getByLabelText('Template name'), { target: { value: 'candidate_default' } })
    fireEvent.change(screen.getByLabelText('Subject'), { target: { value: 'Role {role}' } })
    fireEvent.change(screen.getByLabelText('Body (HTML)'), { target: { value: '<p>Hi {candidate_name</p>' } })
    expect(screen.getByText(/Unmatched or formatted brace/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Body (HTML)'), { target: { value: '<p>Hi {candidate_name}</p>' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create template' }))
    expect(await screen.findByText('candidate_default v1 saved and set as the default template, because no default existed.')).toBeTruthy()
  })

  it('reports a saved version when setting the first default fails', async () => {
    ;(api as unknown as { state: { emailTemplates: unknown[] } }).state.emailTemplates = []
    const spy = vi.spyOn(api, 'setDefaultEmailTemplate').mockRejectedValueOnce(new Error('Network down'))
    renderApp('/occ/email-templates')
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    fireEvent.change(screen.getByLabelText('Template name'), { target: { value: 'welcome' } })
    fireEvent.change(screen.getByLabelText('Subject'), { target: { value: 'Role {role}' } })
    fireEvent.change(screen.getByLabelText('Body (HTML)'), { target: { value: '<p>Hi</p>' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create template' }))
    expect(await screen.findByText(/welcome v1 saved, but the default could not be checked or set \(Network down\)/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Create template' })).toBeNull()
    spy.mockRestore()
  })

  it('blocks a new template that reuses an existing name', async () => {
    renderApp('/occ/email-templates')
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    fireEvent.change(screen.getByLabelText('Template name'), { target: { value: 'candidate_default' } })
    expect(screen.getByText(/A template with this name exists/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Create template' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
