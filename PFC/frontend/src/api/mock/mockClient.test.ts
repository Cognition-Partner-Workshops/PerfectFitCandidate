import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../client'
import { MockApiClient } from './mockClient'

const ADMIN = { fullName: 'Olivia Admin', email: 'olivia.admin@example.com', password: 'Str0ng!Passphrase#42' }
let api: MockApiClient

async function code(p: Promise<unknown>): Promise<string | undefined> {
  try {
    await p
    return undefined
  } catch (e) {
    return e instanceof ApiError ? e.code : 'UNKNOWN'
  }
}

beforeEach(async () => {
  api = new MockApiClient({ persist: false, latencyMs: 0 })
  await api.setupAdmin({ setupCode: api.setupCode, ...ADMIN })
})

describe('mock auth (REQ-AUTH-01..04)', () => {
  it('rejects a wrong setup code and a second admin setup', async () => {
    const fresh = new MockApiClient({ persist: false })
    expect(await code(fresh.setupAdmin({ setupCode: 'WRONG', ...ADMIN }))).toBe('SETUP_CODE_INVALID')
    expect(await code(api.setupAdmin({ setupCode: api.setupCode, ...ADMIN }))).toBe('SETUP_DONE')
  })
  it('uses a generic error for a role mismatch', async () => {
    expect(await code(api.login({ role: 'Analyst', email: ADMIN.email, password: ADMIN.password }))).toBe('INVALID_CREDENTIALS')
  })
  it('locks after five failures', async () => {
    for (let i = 0; i < 4; i++) expect(await code(api.login({ role: 'Admin', email: ADMIN.email, password: 'nope' }))).toBe('INVALID_CREDENTIALS')
    expect(await code(api.login({ role: 'Admin', email: ADMIN.email, password: 'nope' }))).toBe('ACCOUNT_LOCKED')
    expect(await code(api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password }))).toBe('ACCOUNT_LOCKED')
  })
  it('forces a password change for one-time passwords and blocks Analysts from OCC', async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    const otp = await api.createUser({ fullName: 'Bea Analyst', email: 'bea@example.com', role: 'Analyst' })
    expect(await code(api.updateUser(api.state.users[0]?.userId ?? 0, { role: 'Analyst' }))).toBe('LAST_ADMIN')
    await api.logout()
    const u = await api.login({ role: 'Analyst', email: 'BEA@example.com ', password: otp.oneTimePassword })
    expect(u.mustChangePassword).toBe(true)
    expect(await code(api.listUsers())).toBe('FORBIDDEN')
  })
})

describe('mock manual identity resolution (REQ-IN-23)', () => {
  beforeEach(async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
  })

  it('sets the name, replaces manual emails and keeps the email when omitted', async () => {
    const detail = await api.resolveIdentity(10, { action: 'manual_entry', name: '  Morgan Synthetic  ', email: ' A@example.com ' })
    expect(detail.resource.name).toBe('Morgan Synthetic')
    expect(detail.identityStatus).toBe('complete')
    const raw = api.state.raws.find((r) => r.resourceId === 10)
    expect(raw).toMatchObject({ email: 'A@example.com', emailSource: 'manual', emailConfirmed: true })
    await api.resolveIdentity(10, { action: 'manual_entry', name: 'Morgan Synthetic', email: 'B@example.com' })
    await api.resolveIdentity(10, { action: 'manual_entry', name: 'Morgan Renamed' })
    expect(raw).toMatchObject({ name: 'Morgan Renamed', email: 'B@example.com', emailSource: 'manual', identityStatus: 'complete' })
    const audit = JSON.stringify(await api.auditLog({}))
    expect(audit).not.toContain('Morgan Synthetic')
    expect(audit).not.toContain('B@example.com')
  })

  it('completes name-only entry without inventing an email', async () => {
    const detail = await api.resolveIdentity(10, { action: 'manual_entry', name: 'Morgan Synthetic' })
    expect(detail.identityStatus).toBe('complete')
    expect(api.state.raws.find((r) => r.resourceId === 10)?.email).toBeNull()
  })

  it('refuses CT authority and confirmed-email conflicts without changing the candidate', async () => {
    expect(await code(api.resolveIdentity(1, { action: 'manual_entry', name: 'Other' }))).toBe('IDENTITY_CT_AUTHORITATIVE')
    const target = api.state.raws.find((r) => r.resourceId === 10)
    const other = api.state.raws.find((r) => r.resourceId === 1)
    if (!other?.email) throw new Error('missing CT fixture')
    other.emailConfirmed = true
    const before = structuredClone(target)
    expect(await code(api.resolveIdentity(10, { action: 'manual_entry', name: 'Other', email: ` ${other.email.toUpperCase()} ` }))).toBe('IDENTITY_CONFLICT')
    expect(target).toEqual(before)
  })

  it('rejects missing/blank/overlong names and invalid emails', async () => {
    for (const fields of [{}, { name: ' ' }, { name: 'x'.repeat(201) }, { name: 'Morgan', email: 'bad' }, { name: 'Morgan', email: ' ' }]) {
      expect(await code(api.resolveIdentity(10, { action: 'manual_entry', ...fields }))).toBe('VALIDATION_ERROR')
    }
  })
})

describe('mock email approval (REQ-EML-01/06)', () => {
  it('requires approval before send and guards duplicate sends', async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    const { items } = await api.listEmails()
    const draft = items.find((e) => e.status === 'Draft')
    if (!draft) throw new Error('no draft')
    expect(await code(api.sendEmail(draft.emailId))).toBe('EMAIL_NOT_APPROVED')
    await api.approveEmail(draft.emailId)
    const sent = await api.sendEmail(draft.emailId)
    expect(sent.status).toBe('Sent')
    const [again] = await api.createDrafts([draft.resultId])
    expect(again?.previouslySentAt).toBeTruthy()
    expect(await code(api.approveEmail(again?.emailId ?? 0))).toBe('EMAIL_DUPLICATE_PAIR')
    expect((await api.approveEmail(again?.emailId ?? 0, true)).status).toBe('Approved')
  })
  it('pre-selects exactly the top 4 per requisition', async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    const { items } = await api.listEmails()
    const t1 = items.filter((e) => e.codeRequisition === 'T1A123')
    expect(t1.length).toBe(5)
    expect(t1.filter((e) => e.preselected).map((e) => e.rank)).toEqual([1, 2, 3, 4])
  })
})

describe('mock matching (REQ-ELIG-01..07, REQ-MAT-03)', () => {
  it('never shortlists excluded people and keeps at most five per requisition', () => {
    const results = api.computeResults()
    const excluded = new Set(api.resources().filter((r) => r.eligibility === 'excluded' || r.eligibility === 'insufficient').map((r) => r.resourceId))
    expect(results.some((r) => excluded.has(r.resourceId))).toBe(false)
    const per = new Map<string, number>()
    results.forEach((r) => per.set(r.codeRequisition, (per.get(r.codeRequisition) ?? 0) + 1))
    expect(Math.max(...per.values())).toBeLessThanOrEqual(5)
  })
  it('keeps unmapped statuses eligible at priority 2 and flags them', () => {
    const r = api.resources().find((x) => x.benchStatus === 'Bench - awaiting role')
    expect(r?.eligibility).toBe('eligible')
    expect(r?.benchStatusPriority).toBe(2)
    expect(r?.unmappedStatus).toBe(true)
  })
})

describe('mock settings packages (REQ-OCC-01/02)', () => {
  it('previews, applies atomically and rolls back', async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    const bad = await api.previewPackage(new File(['x'], 'PFC-settings-v5.pfcpkg'))
    expect(bad.errors.length).toBeGreaterThan(0)
    expect(await code(api.importPackage({ previewId: bad.previewId }))).toBe('PACKAGE_INVALID')
    const tampered = await api.previewPackage(new File(['x'], 'PFC-settings-v9-tampered.pfcpkg'))
    expect(tampered.signatureValid).toBe(false)
    const good = await api.previewPackage(new File(['x'], 'PFC-settings-v7.pfcpkg'))
    expect(good.errors).toEqual([])
    const history = await api.importPackage({ previewId: good.previewId })
    expect(history[0]?.version).toBe(7)
    expect(history[0]?.canRollback).toBe(true)
    const after = await api.rollbackPackage()
    expect(after[0]).toMatchObject({ version: 7, status: 'rolled_back', isCurrent: false })
    expect(after.find((h) => h.isCurrent)?.version).toBe(6)
    expect(await code(api.rollbackPackage())).toBe('PACKAGE_NO_ROLLBACK')
  })
  it('rolls back more than one step', async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    for (const v of [7, 8]) await api.importPackage({ previewId: (await api.previewPackage(new File(['x'], `PFC-settings-v${v}.pfcpkg`))).previewId })
    expect((await api.rollbackPackage()).find((h) => h.isCurrent)?.version).toBe(7)
    expect((await api.rollbackPackage()).find((h) => h.isCurrent)?.version).toBe(6)
    expect(await code(api.rollbackPackage())).toBe('PACKAGE_NO_ROLLBACK')
  })
  it('exports a monotonic signed package', async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    expect(await code(api.exportPackage(''))).toBe('PASSPHRASE_REQUIRED')
    const a = await api.exportPackage('signing-pass')
    const b = await api.exportPackage('signing-pass')
    expect(b.version).toBe(a.version + 1)
    expect(a.fileName).toBe(`PFC-settings-v${a.version}.pfcpkg`)
    expect(a.signed).toBe(true)
  })
})

describe('mock export (REQ-OUT-09)', () => {
  it('masks emails by default and exports verbatim emails on request', async () => {
    await api.login({ role: 'Admin', email: ADMIN.email, password: ADMIN.password })
    const [run] = await api.listMatchRuns()
    const [first] = await api.getResults(run.runId)
    const email = await api.unmask('resource', first.resourceId, 'email')
    const text = (b: Blob) =>
      new Promise<string>((resolve) => {
        const r = new FileReader()
        r.onload = () => resolve(String(r.result))
        r.readAsText(b)
      })
    const masked = await text((await api.exportResults(run.runId, {}, 'csv', false)).blob)
    const full = await text((await api.exportResults(run.runId, {}, 'csv', true)).blob)
    expect(masked.includes(email)).toBe(false)
    expect(full.includes(email)).toBe(true)
    const log = JSON.stringify(await api.auditLog({}))
    expect(log.includes('Unmasked emails in results export')).toBe(true)
    expect(log.includes(`Exported results for run ${run.runId}`)).toBe(true)
  })
})
