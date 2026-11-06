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
