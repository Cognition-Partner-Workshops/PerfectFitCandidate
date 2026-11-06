import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { ADMIN, ANALYST_PASSWORD, renderApp, seed, signIn } from '../../test/utils'
import { api } from '../../api'
import { auditToCsv, AUDIT_COLUMNS } from '../../lib/csv'
import { gradeWindow, moveItem } from '../../lib/occ'
import { toImportRecord } from '../../api/http'

describe('OCC helpers', () => {
  it('computes the ±1 grade window over active grades (REQ-GRD-01)', () => {
    const g = ['C1', 'C2', 'D1', 'D2', 'E1'].map((code) => ({ code, retired: code === 'D2' }))
    expect(gradeWindow(g, 'D1')).toEqual(['C2', 'D1', 'E1'])
    expect(gradeWindow(g, 'C1')).toEqual(['C1', 'C2'])
    expect(gradeWindow(g, 'Z9')).toEqual([])
  })

  it('moves items and ignores out-of-range moves', () => {
    expect(moveItem(['a', 'b', 'c'], 0, 1)).toEqual(['b', 'a', 'c'])
    expect(moveItem(['a', 'b'], 0, -1)).toEqual(['a', 'b'])
  })

  it('exports audit CSV with fixed columns and formula guard (REQ-AUTH-05)', () => {
    const csv = auditToCsv([{ auditId: 1, at: '2026-11-05T10:00:00Z', user: 'OCC Admin', role: 'Admin', action: 'update', entity: 'grade_list', summary: '=HYPERLINK("x")' }])
    const [header, row] = csv.trim().split('\n')
    expect(header).toBe(AUDIT_COLUMNS.join(','))
    expect(row).toContain(`"'=HYPERLINK(""x"")"`)
  })

  it('maps backend package history rows (agent E shape)', () => {
    expect(toImportRecord({ version: 6, applied_at: '2026-11-01T09:00:00Z', applied_by: 'OCC Admin', status: 'applied', current: false })).toEqual({
      version: 6,
      importedAt: '2026-11-01T09:00:00Z',
      importedBy: 'OCC Admin',
      fileName: 'v6',
      canRollback: true,
    })
    expect(toImportRecord({ version: 7, applied_at: 'x', applied_by: 'y', status: 'applied', current: true }).canRollback).toBe(false)
  })
})

describe('OCC mock API (GUI_OCC_DESIGN.md §16.4/§16.8/§16.10/§16.12/§16.14)', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })

  it('requires a reason to modify or revert, and audits decisions', async () => {
    await expect(api.decideApproval(501, { action: 'revert' })).rejects.toMatchObject({ problem: { code: 'REASON_REQUIRED' } })
    const e = await api.decideApproval(501, { action: 'modify', comment: 'Client confirmed date', values: { 'Position Start Date': '2026-12-20' } })
    expect(e.state).toBe('Modified')
    expect(e.fields[0].after).toBe('2026-12-20')
    await expect(api.decideApproval(501, { action: 'approve' })).rejects.toMatchObject({ problem: { code: 'ALREADY_DECIDED' } })
    const log = await api.auditLog({ entity: 'demand' })
    expect(log[0].summary).toContain('T1A123: Modified')
  })

  it('validates grade codes and uniqueness', async () => {
    const g = await api.listGrades()
    expect(g.map((x) => x.code)).toEqual(['A3', 'A4', 'B1', 'B2', 'C1', 'C2', 'D1', 'D2', 'E1'])
    await expect(api.saveGrades([...g, { code: 'C2', order: 10, retired: false }])).rejects.toMatchObject({ problem: { code: 'GRADE_DUPLICATE' } })
    await expect(api.saveGrades([{ code: 'XYZ', order: 1, retired: false }])).rejects.toMatchObject({ problem: { code: 'GRADE_FORMAT' } })
    const saved = await api.saveGrades([...g, { code: 'e2', order: 10, retired: false }])
    expect(saved.at(-1)).toEqual({ code: 'E2', order: 10, retired: false })
  })

  it('keeps one default per purpose and protects the default (REQ-LLM-01)', async () => {
    await expect(api.updateOccModel('qwen3-4b', { approved: false })).rejects.toMatchObject({ problem: { code: 'MODEL_IS_DEFAULT' } })
    await expect(api.updateOccModel('llama32-3b', { isDefault: true })).rejects.toMatchObject({ problem: { code: 'MODEL_NOT_APPROVED' } })
    const models = await api.updateOccModel('phi4-mini', { isDefault: true })
    expect(models.filter((m) => m.purpose === 'generation' && m.isDefault).map((m) => m.id)).toEqual(['phi4-mini'])
    expect(models.find((m) => m.id === 'nomic-embed')?.isDefault).toBe(true)
  })

  it('rejects admin settings below policy floors (TDD §9.2, REQ-OUT-03)', async () => {
    const s = await api.getAdminSettings()
    expect(s).toMatchObject({ lockoutAttempts: 5, lockoutMinutes: 15, idleTimeoutMinutes: 60, topN: 5, emailsPreselected: 4 })
    await expect(api.saveAdminSettings({ ...s, adminMinPasswordLength: 12 })).rejects.toMatchObject({ problem: { code: 'VALIDATION' } })
    await expect(api.saveAdminSettings({ ...s, maskCount: 4 })).rejects.toMatchObject({ problem: { code: 'VALIDATION' } })
    await expect(api.saveAdminSettings({ ...s, emailsPreselected: 6 })).rejects.toMatchObject({ problem: { code: 'VALIDATION' } })
    expect((await api.saveAdminSettings({ ...s, idleTimeoutMinutes: 30 })).idleTimeoutMinutes).toBe(30)
  })

  it('filters the audit log by date, user and action', async () => {
    await api.saveAdminSettings({ ...(await api.getAdminSettings()), staleDataDays: 10 })
    expect((await api.auditLog({ action: 'backup' })).map((a) => a.user)).toEqual(['System'])
    expect((await api.auditLog({ to: '2026-10-31' })).every((a) => a.at < '2026-11-01')).toBe(true)
    expect((await api.auditLog({ user: 'olivia', entity: 'admin_settings' })).length).toBe(1)
  })

  it('denies Analysts every new OCC endpoint (REQ-ROLE-03)', async () => {
    const { analystEmail } = await seed({ analyst: true })
    await signIn('Analyst', analystEmail, ANALYST_PASSWORD)
    for (const call of [() => api.listApprovals(), () => api.listGrades(), () => api.listOccModels(), () => api.auditLog({}), () => api.getAdminSettings()]) {
      await expect(call()).rejects.toMatchObject({ status: 403 })
    }
  })
})

describe('OCC screens', () => {
  beforeEach(async () => {
    await seed()
    await signIn('Admin', ADMIN.email, ADMIN.password)
  })

  it('lists the new OCC sections in the sub-navigation', async () => {
    renderApp('/occ/overview')
    const nav = await screen.findByRole('navigation', { name: 'OCC sections' })
    for (const label of ['DL / CT approvals', 'Grade list', 'Approved models', 'Audit log', 'Admin settings']) expect(within(nav).getByText(label)).toBeTruthy()
  })

  it('approves a DL change and requires a reason to revert a CT change', async () => {
    renderApp('/occ/approvals')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve T1A123' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve T1A123' })).toBeNull())
    fireEvent.click(screen.getByRole('tab', { name: /Candidate Tracker/ }))
    expect(screen.getByText('⚠ Conflict')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Revert ALEXANDER/ }))
    const confirm = screen.getByRole('button', { name: 'Revert change' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Status set by Admin' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(screen.queryByRole('button', { name: /Revert ALEXANDER/ })).toBeNull())
  })

  it('previews the ±1 window and reorders grades', async () => {
    renderApp('/occ/grades')
    expect((await screen.findByTestId('grade-window')).textContent).toBe('D1 demand → C2, D1, D2')
    fireEvent.click(screen.getByRole('button', { name: 'Retire D2' }))
    expect(screen.getByTestId('grade-window').textContent).toBe('D1 demand → C2, D1, E1')
    fireEvent.click(screen.getByRole('button', { name: 'Save grade list' }))
    expect(await screen.findByText(/Grade list saved/)).toBeTruthy()
  })

  it('blocks out-of-range admin settings before saving', async () => {
    renderApp('/occ/admin-settings')
    const input = await screen.findByLabelText('Admin minimum password length')
    fireEvent.change(input, { target: { value: '8' } })
    expect(screen.getByText(/Out of range: Admin minimum password length/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save admin settings' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('filters the audit log and enables CSV export', async () => {
    renderApp('/occ/audit-log')
    const table = await screen.findByRole('table', { name: 'Audit events' })
    expect(within(table).getByText('Nightly backup completed')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'backup' } })
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Audit events' })).queryByText('Scoring template v3 published')).toBeNull())
    expect((screen.getByRole('button', { name: /Export CSV \(1\)/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('protects the default model from revocation in the UI', async () => {
    renderApp('/occ/models')
    const box = (await screen.findByLabelText('Approved Qwen3-4B-Instruct-2507 (Q4_K_M)')) as HTMLInputElement
    expect(box.disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Make Llama-3.2-3B-Instruct (Q4_K_M) default' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
