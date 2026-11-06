import type { Band } from '../api/types'

export interface BandThresholds {
  strong: number
  potential: number
  avg: number
}

/** Baseline band thresholds: Strong ≥ 90, Potential 70–89, Avg. 61–69, Weak < 61 (REQ-SCR-02, O-03). */
export const DEFAULT_BANDS: BandThresholds = { strong: 90, potential: 70, avg: 61 }

export function bandFor(score: number, t: BandThresholds = DEFAULT_BANDS): Band {
  const s = Math.round(score)
  if (s >= t.strong) return 'Strong'
  if (s >= t.potential) return 'Potential'
  if (s >= t.avg) return 'Avg'
  return 'Weak'
}

export const BAND_ORDER: Band[] = ['Strong', 'Potential', 'Avg', 'Weak', 'Insufficient data']

export function bandLabel(band: Band): string {
  if (band === 'Avg') return 'Avg.'
  if (band === 'Weak') return 'Weak Match'
  return band
}

export function bandTone(band: Band): 'success' | 'info' | 'warning' | 'danger' | 'neutral' {
  switch (band) {
    case 'Strong':
      return 'success'
    case 'Potential':
      return 'info'
    case 'Avg':
      return 'warning'
    case 'Weak':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function bandColorVar(band: Band): string {
  const map: Record<Band, string> = {
    Strong: '--color-band-strong',
    Potential: '--color-band-potential',
    Avg: '--color-band-avg',
    Weak: '--color-band-weak',
    'Insufficient data': '--color-band-insufficient',
  }
  return `var(${map[band]})`
}
