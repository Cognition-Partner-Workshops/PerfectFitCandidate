import type { Role } from '../api/types'

export interface PasswordCheck {
  id: string
  label: string
  ok: boolean
}

const COMMON = new Set([
  'password',
  'password1',
  'password123',
  'passw0rd',
  'qwerty',
  'qwerty123',
  'letmein',
  'welcome',
  'welcome1',
  'admin',
  'administrator',
  'changeme',
  'iloveyou',
  '12345678',
  '123456789',
  '1234567890',
])

/**
 * Live password checklist (GUI_OCC_DESIGN.md §7.2) for the TDD.md §9.2 policy:
 * Admin ≥ 14 characters and ≥ 3 of 4 character classes; Analyst ≥ 10 characters;
 * both must not contain the email local part / name and must not be a common password.
 * The server remains the authority; this only drives the UI (REQ-AUTH-03).
 */
export function checkPassword(
  password: string,
  role: Role,
  context: { email?: string; name?: string } = {},
): { checks: PasswordCheck[]; ok: boolean } {
  const minLength = role === 'Admin' ? 14 : 10
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(password)).length
  const lower = password.toLowerCase()
  const local = (context.email ?? '').split('@')[0]?.toLowerCase() ?? ''
  const nameParts = (context.name ?? '')
    .toLowerCase()
    .split(/\s+/)
    .filter((p) => p.length >= 3)
  const containsIdentity =
    (local.length >= 3 && lower.includes(local)) || nameParts.some((p) => lower.includes(p))
  const checks: PasswordCheck[] = [
    { id: 'length', label: `at least ${minLength} characters`, ok: password.length >= minLength },
  ]
  if (role === 'Admin') {
    checks.push(
      { id: 'case', label: 'upper and lower case', ok: /[a-z]/.test(password) && /[A-Z]/.test(password) },
      { id: 'number', label: 'a number', ok: /\d/.test(password) },
      { id: 'symbol', label: 'a symbol', ok: /[^A-Za-z0-9]/.test(password) },
      { id: 'classes', label: '3 of 4 character types', ok: classes >= 3 },
    )
  }
  checks.push(
    { id: 'identity', label: 'not the email or name', ok: password.length > 0 && !containsIdentity },
    { id: 'common', label: 'not a common password', ok: password.length > 0 && !COMMON.has(lower) },
  )
  const required = new Set(['length', 'classes', 'identity', 'common'])
  return { checks, ok: checks.filter((c) => required.has(c.id)).every((c) => c.ok) }
}

export function strengthScore(checks: PasswordCheck[]): number {
  return checks.filter((c) => c.ok).length
}
