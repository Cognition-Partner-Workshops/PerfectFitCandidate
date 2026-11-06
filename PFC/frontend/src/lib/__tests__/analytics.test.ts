import { describe, expect, it } from 'vitest'
import { benchAnalytics, demandAnalytics, summarise } from '../analytics'
import { MockApiClient } from '../../api/mock/mockClient'

const client = new MockApiClient({ persist: false })
const demands = client.state.demands.map((d) => ({ ...d, matches: 0, bestBand: null }))
const resources = client.resources()
const results = client.computeResults()

describe('dashboard analytics (REQ-OUT-04..07)', () => {
  it('summarises the header and counts Weak-only requisitions as no suitable candidate (GQ-01)', () => {
    const s = summarise(demands, resources, results, client.state.emails, null)
    expect(s.requisitionsAssessed).toBe(demands.filter((d) => d.jdStatus === 'Scored').length)
    expect(s.roleToPersonCombinations).toBe(results.length)
    expect(s.eligibleBench + s.excludedBench).toBe(resources.length)
    expect(s.rolesWithNoSuitableCandidate).toBeGreaterThanOrEqual(s.weakOnlyRequisitions)
  })
  it('builds Grade → JD count over all nine grades and sorts DL rows by start date desc', () => {
    const d = demandAnalytics(demands)
    expect(d.gradeToJdCount.map((g) => g.label)).toEqual(['A3', 'A4', 'B1', 'B2', 'C1', 'C2', 'D1', 'D2', 'E1'])
    expect(d.gradeToJdCount.reduce((a, b) => a + b.count, 0)).toBe(demands.length)
    expect(d.topSkills.length).toBeLessThanOrEqual(5)
    expect(d.topAccounts.length).toBeLessThanOrEqual(3)
    const dates = d.rows.map((r) => r.positionStartDate ?? '')
    expect(dates).toEqual([...dates].sort().reverse())
  })
  it('splits demanded-skill supply by Available Capacity without using it for eligibility (REQ-OUT-06, REQ-ELIG-06)', () => {
    const b = benchAnalytics(resources, demands, new Date('2026-11-06'))
    expect(b.demandVsSupply.length).toBeLessThanOrEqual(5)
    for (const row of b.demandVsSupply) {
      const holders = resources.filter(
        (r) => (r.eligibility === 'eligible' || r.eligibility === 'eligible_caveat') && r.skills.some((s) => s.toLowerCase() === row.skill.toLowerCase()),
      )
      expect(row.supplyY + row.supplyN).toBe(holders.length)
    }
    const statuses = b.statusDistribution
    const firstExcluded = statuses.findIndex((s) => s.excluded)
    expect(statuses.slice(firstExcluded).every((s) => s.excluded)).toBe(true)
    const starts = b.rows.map((r) => r.benchStartDate ?? '')
    expect(starts).toEqual([...starts].sort().reverse())
  })
})
