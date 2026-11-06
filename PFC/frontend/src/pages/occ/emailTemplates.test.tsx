import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { ADMIN, ANALYST_PASSWORD, renderApp, seed, signIn } from '../../test/utils'
import { api } from '../../api'
import { unknownPlaceholders } from '../../lib/occ'

describe('email template placeholders (REQ-EML-03)', () => {
  it('flags tokens outside the allowlist and keeps literal braces and CSS', () => {
    expect(unknownPlaceholders('Opportunity {role}', '<p>{candidate_name} {spoc_1}</p>')).toEqual([])
    expect(unknownPlaceholders('{Unknown}', '{missing!r} {Unknown}')).toEqual(['Unknown', 'missing!r'])
    expect(unknownPlaceholders('', '<style>a{color:red}</style>{{literal}} {unfinished')).toEqual([])
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

  it('blocks a new template that reuses an existing name', async () => {
    renderApp('/occ/email-templates')
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    fireEvent.change(screen.getByLabelText('Template name'), { target: { value: 'candidate_default' } })
    expect(screen.getByText(/A template with this name exists/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Create template' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
