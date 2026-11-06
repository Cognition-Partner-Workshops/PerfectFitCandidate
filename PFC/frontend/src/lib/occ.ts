import type { ScoringWeights, Synonym } from '../api/types'

/** `@Capgemini.com ` → `capgemini.com` (REQ-CLS-01). Returns null when not a domain. */
export function normaliseDomain(input: string): string | null {
  const d = input.trim().toLowerCase().replace(/^@/, '')
  return /^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(d) ? d : null
}

/** Template weights must total 100 (REQ-SCR-01). */
export function weightTotal(w: ScoringWeights): number {
  return Object.values(w).reduce((a, b) => a + b, 0)
}

/** Resolve a term to its active canonical skill, case-insensitively (REQ-MAT-05). */
export function normaliseTerm(term: string, synonyms: Synonym[]): string | null {
  const t = term.trim().toLowerCase()
  if (!t) return null
  const hit = synonyms.find((s) => s.status === 'Active' && (s.canonical.toLowerCase() === t || s.variants.some((v) => v.toLowerCase() === t)))
  return hit?.canonical ?? null
}

export const EMAIL_SYNTAX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/** ±1 grade window for a demand grade over the active ordered list (REQ-GRD-01, §16.8). */
export function gradeWindow(grades: { code: string; retired: boolean }[], code: string): string[] {
  const active = grades.filter((g) => !g.retired).map((g) => g.code)
  const i = active.indexOf(code)
  return i < 0 ? [] : active.slice(Math.max(0, i - 1), i + 2)
}

/** Move an item within a list (keyboard-accessible reordering). */
export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (to < 0 || to >= items.length || from === to) return items
  const next = [...items]
  const [x] = next.splice(from, 1)
  next.splice(to, 0, x)
  return next
}

/** Placeholders the backend allows in email templates (REQ-EML-03, `pfc.services.occ.templates.PLACEHOLDERS`). */
export const EMAIL_PLACEHOLDERS = ['candidate_name', 'requisition', 'role', 'match_pct', 'analysis', 'gaps', 'pointers', 'spoc_1', 'spoc_2'] as const

/** `{token}`s outside the allowlist, using the backend's rule: `{{`/`}}` and CSS blocks are literal. */
export function unknownPlaceholders(subject: string, body: string): string[] {
  const allowed = new Set<string>(EMAIL_PLACEHOLDERS)
  const tokens = [...`${subject}\n${body}`.matchAll(/(?<!\{)\{([^{}\s:;]+)\}(?!\})/g)].map((m) => m[1])
  return [...new Set(tokens.filter((t) => !allowed.has(t)))]
}
