import type { MatchRun } from '../api/types'

/** "incremental" or "full (reason)", exactly as the backend records the run (REQ-MAT-07). */
export function runMode(run: MatchRun): string | null {
  if (!run.mode) return null
  return run.mode === 'full' && run.modeReason ? `full (${run.modeReason})` : run.mode
}
