import { describe, expect, it } from 'vitest'
import { formatDate, formatPct, initials, roleLocation } from '../format'
import { maskEmail, maskPhone } from '../mask'
import { bandFor, bandLabel } from '../bands'
import { checkPassword } from '../password'
import { canEdit, canView, visibleNav } from '../permissions'
import { applyResultFilters, activeFilterCount } from '../filters'
import { groupByRequisition, hoursLeft, preselectForEmail } from '../emailSelection'
import { RESULT_COLUMNS, resultsToCsv } from '../csv'
import { normaliseDomain, normaliseTerm, weightTotal } from '../occ'
import { unknownPlaceholders } from '../emailTemplate'
import type { MatchResult, Synonym } from '../../api/types'

function result(p: Partial<MatchResult>): MatchResult {
  return {
    resultId: 1, runId: 1, demandId: 1, resourceId: 1, candidateId: 1, rank: 1, codeRequisition: 'T1A123',
    positionStartDate: '2026-12-15', accountName: 'Acme Bank', bu: 'BCM', demandRequestName: 'Role', demandGrade: 'D1',
    region: 'London', personName: 'Alex', personEmailMasked: 'Al******er@example.com', personGrade: 'D1', personLocation: 'London',
    benchStatus: 'Available', score: 93, band: 'Strong', gradeRelation: 'exact', locationAssessment: 'Aligned',
    matchingEvidence: ['SQL'], gapsChecks: ['None'], eligibilityEvidence: ['Bench End Date blank'], mandatoryMissing: 0,
    candidateType: 'Internal', incompleteIdentity: false, emailPreselected: true, breakdown: [], ...p,
  }
}

describe('format (REQ-OUT-02/10, GQ-11)', () => {
  it('formats dates as DD MMM YYYY', () => {
    expect(formatDate('2026-11-04')).toBe('04 Nov 2026')
    expect(formatDate(null)).toBe('Not recorded')
    expect(formatDate('Wed 4/11/2026')).toBe('Unparseable: Wed 4/11/2026')
  })
  it('renders integer percentages and Unspecified locations', () => {
    expect(formatPct(86.6)).toBe('87 %')
    expect(roleLocation('')).toBe('Unspecified')
    expect(initials('Olivia Admin')).toBe('OA')
  })
})

describe('masking (REQ-OUT-03)', () => {
  it('masks 6 characters after the 2nd position', () => {
    expect(maskEmail('AlexBorder@capgemini.com')).toBe('Al******er@capgemini.com')
    expect(maskEmail('ab@x.com')).toBe('**@x.com')
    expect(maskEmail(null)).toBeNull()
  })
  it.each([
    ['a@x.com', '*@x.com'],
    ['ab@x.com', '**@x.com'],
    ['abc@x.com', 'ab*@x.com'],
    ['abcdefg@x.com', 'ab*****@x.com'],
    ['abcdefgh@x.com', 'ab******@x.com'],
    ['abcdefghi@x.com', 'ab******i@x.com'],
    ['🙂@x.com', '*@x.com'],
    ['🙂🙂@x.com', '**@x.com'],
    ['@x.com', '@x.com'],
    ['ab', '**'],
  ])('matches backend masking for %s and keeps masked responses unchanged', (email, expected) => {
    expect(maskEmail(email)).toBe(expected)
    expect(maskEmail(expected)).toBe(expected)
  })
  it('keeps missing presentation values absent', () => {
    expect(maskEmail('')).toBeNull()
    expect(maskEmail(undefined)).toBeNull()
  })
  it('masks phones keeping prefix and last 2 digits', () => {
    expect(maskPhone('+44 7700 900189')).toBe('+44 ******89')
  })
})

describe('bands (REQ-SCR-02, O-03)', () => {
  it('applies baseline thresholds', () => {
    expect(bandFor(90)).toBe('Strong')
    expect(bandFor(89)).toBe('Potential')
    expect(bandFor(70)).toBe('Potential')
    expect(bandFor(69)).toBe('Avg')
    expect(bandFor(61)).toBe('Avg')
    expect(bandFor(60)).toBe('Weak')
    expect(bandLabel('Avg')).toBe('Avg.')
  })
})

describe('password policy (REQ-AUTH-03, TDD §9.2)', () => {
  it('requires ≥ 14 chars and 3 of 4 classes for Admin', () => {
    expect(checkPassword('Short1!', 'Admin').ok).toBe(false)
    expect(checkPassword('alllowercaseletters', 'Admin').ok).toBe(false)
    expect(checkPassword('Str0ng!Passphrase#42', 'Admin').ok).toBe(true)
  })
  it('rejects passwords containing the email local part or a common password', () => {
    expect(checkPassword('olivia.admin!X99zzzz', 'Admin', { email: 'olivia.admin@example.com' }).ok).toBe(false)
    expect(checkPassword('password123', 'Analyst').ok).toBe(false)
    expect(checkPassword('Analyst#Pass2026', 'Analyst').ok).toBe(true)
  })
})

describe('role-visibility matrix (GUI_OCC_DESIGN.md §5)', () => {
  it('hides OCC from Analysts and Chat in Phase 1', () => {
    const analyst = visibleNav('Analyst').map((n) => n.label)
    const admin = visibleNav('Admin').map((n) => n.label)
    expect(analyst).toEqual(['Dashboard', 'Demand List', 'Bench List', 'Match Results', 'Candidate Profile', 'Email Approval', 'Settings'])
    expect(admin).toContain('OCC')
    expect(admin).not.toContain('Chat')
  })
  it('gives Analysts partial model choice and read-only template version', () => {
    expect(canView('Analyst', 'occ')).toBe(false)
    expect(canEdit('Analyst', 'settingsChooseModel')).toBe(false)
    expect(canEdit('Admin', 'settingsChooseModel')).toBe(true)
    expect(canEdit('Analyst', 'scoringTemplateVersion')).toBe(false)
  })
})

describe('result filters (REQ-OUT-08, REQ-MAT-10)', () => {
  const rows = [
    result({ resultId: 1, score: 93, band: 'Strong' }),
    result({ resultId: 2, codeRequisition: 'T2B456', accountName: 'Contoso', bu: 'INS', score: 65, band: 'Avg', mandatoryMissing: 1, candidateType: 'External', incompleteIdentity: true, personGrade: 'C2', personLocation: 'Glasgow', region: 'Glasgow', benchStatus: 'Selected', demandGrade: 'C2' }),
  ]
  it.each([
    [{ codeRequisition: 't2b' }, [2]],
    [{ accountName: 'Acme Bank' }, [1]],
    [{ bu: 'INS' }, [2]],
    [{ demandGrade: 'D1' }, [1]],
    [{ personGrade: 'C2' }, [2]],
    [{ location: 'Glasgow' }, [2]],
    [{ region: 'London' }, [1]],
    [{ band: 'Avg' as const }, [2]],
    [{ status: 'Selected' }, [2]],
    [{ candidateType: 'External' as const }, [2]],
    [{ incompleteIdentity: 'yes' as const }, [2]],
    [{ mandatorySkills: 'met' as const }, [1]],
    [{ scoreMin: 70 }, [1]],
    [{ scoreMax: 70 }, [2]],
  ])('filters %o', (f, ids) => {
    expect(applyResultFilters(rows, f).map((r) => r.resultId)).toEqual(ids)
  })
  it('counts active filters', () => {
    expect(activeFilterCount({ bu: 'INS', band: '' })).toBe(1)
  })
})

describe('email preselection (REQ-EML-01, O-07)', () => {
  it('pre-selects the top 4 of the top 5 per requisition and skips excluded overrides', () => {
    const rows = [1, 2, 3, 4, 5].map((rank) => result({ resultId: rank, rank }))
    rows.push(...[1, 2].map((rank) => result({ resultId: 10 + rank, rank, codeRequisition: 'T2B456' })))
    expect([...preselectForEmail(rows)].sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 11, 12])
    const excluded = rows.map((r) => (r.resultId === 2 ? { ...r, override: { action: 'exclude' as const, reason: 'x', createdBy: 'a', createdAt: '' } } : r))
    expect(preselectForEmail(excluded).has(2)).toBe(false)
    expect(preselectForEmail(excluded).has(5)).toBe(true)
  })
  it('groups by requisition in rank order and computes the 24 h window', () => {
    const g = groupByRequisition([result({ rank: 2, resultId: 2 }), result({ rank: 1, resultId: 1 })])
    expect(g[0]?.[1].map((r) => r.rank)).toEqual([1, 2])
    expect(hoursLeft('2026-11-05T00:00:00Z', new Date('2026-11-05T18:00:00Z'))).toBe(6)
    expect(hoursLeft('2026-11-03T00:00:00Z', new Date('2026-11-05T00:00:00Z'))).toBeLessThan(0)
  })
})

describe('CSV export (REQ-OUT-01/09)', () => {
  it('uses the 16 columns in order with masked email after the name, metadata and disclaimer', () => {
    expect(RESULT_COLUMNS).toHaveLength(16)
    expect(RESULT_COLUMNS.indexOf('Person Email')).toBe(RESULT_COLUMNS.indexOf('Potential Person') + 1)
    const lines = resultsToCsv([result({ accountName: '=HYPERLINK("x")' })], 'Run meta').split('\n')
    expect(lines[0]).toBe('Run meta')
    expect(lines[1]).toContain('human review')
    expect(lines[3]).toContain('Al******er@example.com')
    expect(lines[3]).toContain(`"'=HYPERLINK(""x"")"`)
  })
})

describe('OCC helpers', () => {
  it('normalises internal domains (REQ-CLS-01)', () => {
    expect(normaliseDomain(' @Capgemini.com ')).toBe('capgemini.com')
    expect(normaliseDomain('not a domain')).toBeNull()
  })
  it('totals weights (REQ-SCR-01)', () => {
    expect(weightTotal({ skills: 70, grade: 10, location: 5, benchStart: 5, benchEnd: 5, status: 5 })).toBe(100)
  })
  it('normalises synonyms (REQ-MAT-05)', () => {
    const syn: Synonym[] = [{ synonymId: 1, canonical: 'Business Analysis', variants: ['BA'], relation: 'Abbreviation', usageCount: 0, source: 'seed', status: 'Active' }]
    expect(normaliseTerm('ba', syn)).toBe('Business Analysis')
    expect(normaliseTerm('PM', syn)).toBeNull()
  })
  it('flags unknown template placeholders (REQ-EML-03)', () => {
    expect(unknownPlaceholders('Hi {candidate_name} {bogus}')).toEqual(['{bogus}'])
  })
})
