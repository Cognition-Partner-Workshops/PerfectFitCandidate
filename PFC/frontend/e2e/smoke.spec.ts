import { copyFileSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

/**
 * End-to-end smoke test against the real backend (REQ-AUTH-01/02, REQ-ING-01..03, REQ-OUT-03..07, REQ-EML-03).
 * `e2e/serve.py` starts FastAPI on a fresh data dir with synthetic DL/CT workbooks and the
 * backend tests' tiny stand-in ONNX embedder as the active model, so a match run completes.
 */
const data = fileURLToPath(new URL('./.data/', import.meta.url))
const EMAIL = 'e2e.owner@example.com'
const PASSWORD = 'Ledger-River-Passw0rd!'

async function upload(page: import('@playwright/test').Page, button: string, label: string, file: string) {
  await page.getByRole('button', { name: button }).click()
  await page.getByLabel(label).setInputFiles(`${data}fixtures/${file}`)
  const commit = page.getByRole('button', { name: /^Commit \d+ rows$/ })
  await expect(commit).toBeVisible()
  await expect(page.getByText('Rows read 2')).toBeVisible()
  await commit.click()
  await expect(commit).toBeHidden()
}

async function signIn(page: import('@playwright/test').Page) {
  const form = page.getByRole('form', { name: 'Sign in' })
  await form.getByRole('combobox').selectOption('Admin')
  await form.locator('input[type="email"]').fill(EMAIL)
  await form.locator('#login-password').fill(PASSWORD)
  await form.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
}

/** Waits on the run's own status (no fixed sleeps): queued/running → completed. */
async function waitForCompletedRun(page: import('@playwright/test').Page) {
  await expect
    .poll(
      async () => {
        const runs = (await (await page.request.get('/api/v1/match-runs')).json()) as { status: string }[]
        return runs[0]?.status
      },
      { timeout: 30_000, intervals: [250, 500, 1000] },
    )
    .toBe('completed')
}

test('first-admin setup, sign in, DL/CT upload and commit, bench list', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Create the OCC administrator account' })).toBeVisible()
  await page.getByLabel('Setup code').fill(readFileSync(`${data}run/setup-code.txt`, 'utf8').trim())
  await page.getByLabel('Full name').fill('Test Owner')
  await page.getByLabel('Corporate email').fill(EMAIL)
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
  await page.getByLabel('Confirm password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Create admin' }).click()
  await page.getByRole('button', { name: 'Next' }).click()
  await page.getByRole('button', { name: 'Go to sign in' }).click()

  await signIn(page)
  // Dashboard reads the four /analytics/* endpoints; on an empty data dir it must load without an error.
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
  await expect(page.getByText('Requisitions assessed')).toBeVisible()
  await expect(page.getByText('Not Found')).toHaveCount(0)

  await page.goto('/#/demands')
  await expect(page.getByRole('heading', { name: 'Demand List' })).toBeVisible()
  await upload(page, 'Upload DL', 'Demand List workbook', 'dl.xlsx')
  await expect(page.getByText('T10001')).toBeVisible()

  await page.goto('/#/bench')
  await expect(page.getByRole('heading', { name: 'Bench List' })).toBeVisible()
  await upload(page, 'Upload CT', 'Candidate Tracker workbook', 'ct.xlsx')
  await expect(page.getByText('SmokeAlex')).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Eligible (1)' })).toBeVisible()
  // CT rows now carry candidate_id / cv_versions_count (REQ-IN-22): no CVs synced yet, and ShortName opens the profile.
  await expect(page.getByRole('row', { name: /SmokeAlex/ }).getByText('Not synced')).toBeVisible()
  await page.getByRole('link', { name: 'SmokeAlex' }).click()
  await expect(page.getByRole('heading', { name: /SmokeAlex/ })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'CV versions (0)' })).toBeVisible()
  const classification = page.getByRole('region', { name: 'Candidate classification' })
  await expect(classification.getByRole('button', { name: 'Set override' })).toBeDisabled()
  await classification.getByLabel('Override classification').selectOption('Unknown')
  await classification.getByLabel(/Classification reason/).fill('Smoke review')
  await classification.getByRole('button', { name: 'Set override' }).click()
  await expect(classification.getByText('Reason: Smoke review')).toBeVisible()
  await page.reload()
  await expect(classification.getByText('Reason: Smoke review')).toBeVisible()
  await expect(classification.getByText(/Computed classification:/)).toBeVisible()
  await classification.getByLabel(/Classification reason/).fill('Return to automatic')
  await classification.getByRole('button', { name: 'Clear override' }).click()
  await expect(classification.getByText('Reason: Smoke review')).toBeHidden()
  await page.goto('/#/bench')
  // C1 is not in the seeded grade list, so the second synthetic row is excluded from matching.
  await page.getByRole('tab', { name: 'Excluded from matching (1)' }).click()
  await expect(page.getByText('SmokeTaylor')).toBeVisible()

  await page.goto('/#/dashboard')
  await expect(page.getByText('Requisitions assessed')).toBeVisible()
  await expect(page.getByText('Not Found')).toHaveCount(0)

  // Analyze JD → Confirm & Start → completed run (REQ-MAT-02/08), then Match Results (REQ-OUT-01/03/07, REQ-ELIG-08).
  await page.goto('/#/demands')
  const demandRow = page.getByRole('row', { name: /T10001/ })
  await expect(demandRow).toContainText('Not analysed')
  await demandRow.getByRole('button', { name: 'Analyze JD' }).click()
  const start = page.getByRole('button', { name: /Confirm & Start/ })
  // Analyze JD runs server-side (local LLM, or the rule-based fallback once the provider is unavailable); allow for slow CI.
  await expect(start).toBeEnabled({ timeout: 30_000 })
  await expect(page.getByText(/DL region:/)).toBeVisible()
  await page.getByLabel(/Qualifications/).fill('BSc Computing')
  await page.getByLabel('Location (extracted from JD)').fill('Manchester')
  await start.click()
  await expect(start).toBeHidden()
  await waitForCompletedRun(page)
  await expect(demandRow).toContainText('Confirmed')
  await demandRow.getByRole('button', { name: 'Review JD' }).click()
  await expect(page.getByLabel(/Qualifications/)).toHaveValue('BSc Computing')
  await expect(page.getByLabel('Location (extracted from JD)')).toHaveValue('Manchester')
  await page.getByRole('button', { name: 'Close' }).click()

  await page.goto('/#/matches')
  const result = page.getByRole('row', { name: /SmokeAlex/ })
  await expect(result.getByRole('img', { name: /^Match \d+ percent, / })).toBeVisible()
  // No CV is synced, so the band is usually "Insufficient data"; any band label proves the column renders.
  await expect(result.locator('.chip').filter({ hasText: /^(Strong|Potential|Avg\.|Weak Match|Insufficient data)$/ })).toBeVisible()
  await expect(result).toContainText(/SmokeAlex\s*a[a-z]*\*+@example\.com/)
  await expect(result).not.toContainText('alex@example.com')

  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export CSV' }).click()
  const csv = readFileSync(await (await download).path(), 'utf8')
  expect(csv).toMatch(/a[a-z]*\*+@example\.com/)
  expect(csv).not.toContain('alex@example.com')

  await page.getByRole('tab', { name: /^Excluded from matching/ }).click()
  await expect(page.getByRole('table', { name: 'Excluded from matching' }).getByText('SmokeTaylor')).toBeVisible()

  // OCC email templates (REQ-EML-03): create v1 and make it the default against the real routes.
  await page.goto('/#/occ/email-templates')
  await page.getByRole('button', { name: 'New template' }).click()
  await page.getByLabel('Template name').fill('smoke_template')
  await page.getByLabel('Subject').fill('Opportunity {requisition}')
  await page.getByLabel('Body (HTML)').fill('<p>Hi {candidate_name}</p>')
  await page.getByRole('button', { name: 'Create template' }).click()
  await expect(page.getByText('smoke_template v1 saved.', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'Set smoke_template v1 as default' }).click()
  await expect(page.getByText('smoke_template v1 is now the default template.')).toBeVisible()

  // A DL/CT commit after the run flags it as stale (REQ-MAT-08).
  await page.goto('/#/bench')
  await upload(page, 'Upload CT', 'Candidate Tracker workbook', 'ct-update.xlsx')
  const committedAt = Date.now()
  await page.goto('/#/matches')
  await page.reload()
  await expect(page.getByText('Data changed since this run')).toBeVisible()
  await expect(page.getByText(/DL\/CT committed/)).toBeVisible()

  // Re-run changed only (REQ-MAT-07): the banner clears once the new run completes, and the mode is shown.
  const latestRun = async () => ((await (await page.request.get('/api/v1/match-runs')).json()) as { runId: number; status: string }[])[0]
  const before = (await latestRun())?.runId
  // Whole-second commit stamps count as changed until the end of their second, so a run finishing
  // within that second would still be flagged; real re-runs are never that fast.
  await page.waitForTimeout(Math.max(0, committedAt + 1100 - Date.now()))
  await page.getByRole('button', { name: 'Re-run changed only' }).click()
  await expect.poll(async () => { const r = await latestRun(); return r && r.runId !== before ? r.status : 'pending' }, { timeout: 30_000, intervals: [250, 500, 1000] }).toBe('completed')
  await page.reload()
  await expect(page.getByText(/Mode: (incremental|full)/)).toBeVisible()
  await expect(page.getByText('Data changed since this run')).toHaveCount(0)

  // Candidate Profile with match results: it loads, and the Matches tab lists the run's result with its evidence.
  await page.goto('/#/bench')
  await page.getByRole('link', { name: 'SmokeAlex' }).click()
  await expect(page.getByRole('heading', { name: /SmokeAlex/ })).toBeVisible()
  await expect(page.getByText(/Something went wrong/)).toHaveCount(0)
  await page.getByRole('tab', { name: 'Matches & score history' }).click()
  // One row per run: the first run and its re-run.
  await expect(page.getByRole('row', { name: /T10001/ }).first()).toBeVisible()
  // The Matches tab doesn't render evidence, so read it from the same profile response.
  const alexId = page.url().split('/').pop()!.split('?')[0]
  const profile = (await (await page.request.get(`/api/v1/candidates/${alexId}`)).json()) as { matches: { matchingEvidence: string[] }[] }
  expect(profile.matches.length).toBeGreaterThan(0)
  for (const m of profile.matches) expect(m.matchingEvidence.filter((e) => e.trim()).length).toBeGreaterThan(0)
})

// REQ-LLM-02 summary cards. Runs after the smoke test above, on its admin, DL/CT and confirmed JD.
// Uses SmokeTaylor (C1, listed under All), whose CV fixtures carry the email and phone; the smoke test
// above covers a profile with match results (SmokeAlex).
// `serve.py` injects the backend's FakeProvider as a summaries-only stub, with one reply for every
// source: the backend keeps only the sentences whose evidence is in that source.
const LABEL = 'AI summary for reading only — not used in scoring. Check against the source.'
const REDACTED = 'Reach Taylor at [email removed] or [phone removed].'
const CV_EMAIL = 'taylor.longname-consultant@example.com'
const CV_PHONE = '07700 900123'

async function syncTaylorCv(page: import('@playwright/test').Page, candidateId: string, versions: number) {
  await page.goto('/#/bench')
  await page.getByRole('tab', { name: /^All \(/ }).click()
  await page.getByRole('row', { name: /SmokeTaylor/ }).getByRole('button', { name: 'Sync', exact: true }).click()
  await expect
    .poll(async () => ((await (await page.request.get(`/api/v1/candidates/${candidateId}/cv-versions`)).json()) as unknown[]).length, {
      timeout: 30_000,
      intervals: [250, 500, 1000],
    })
    .toBe(versions)
}

async function expectEvidence(card: import('@playwright/test').Locator, count: number) {
  await expect(card.getByText(LABEL, { exact: true })).toBeVisible()
  const sentences = card.locator('details')
  await expect(sentences).toHaveCount(count)
  for (let i = 0; i < count; i++) {
    const evidence = sentences.nth(i)
    await expect(evidence.locator('blockquote')).toBeHidden()
    await evidence.getByText('Evidence', { exact: true }).click()
    await expect(evidence.locator('blockquote')).toBeVisible()
    await expect(evidence.locator('blockquote')).not.toBeEmpty()
  }
}

test('summary cards: grounded evidence, stale source, provider unavailable, no raw PII (REQ-LLM-02)', async ({ page }) => {
  await page.goto('/')
  await signIn(page)
  await page.goto('/#/settings/data')
  await page.getByLabel('Resume folder path').fill(`${data}fixtures/resumes`)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Saved', { exact: true })).toBeVisible()

  await page.goto('/#/bench')
  await page.getByRole('tab', { name: /^All \(/ }).click()
  await page.getByRole('link', { name: 'SmokeTaylor' }).click()
  await expect(page.getByRole('heading', { name: /SmokeTaylor/ })).toBeVisible()
  const candidateId = page.url().split('/').pop()!.split('?')[0]
  await syncTaylorCv(page, candidateId, 1)

  // Candidate Profile: generate, label, every sentence's evidence expands; the CV's email and phone never show raw.
  await page.goto(`/#/candidates/${candidateId}`)
  const card = page.getByRole('region', { name: 'Summary' })
  await card.getByRole('button', { name: 'Generate summary' }).click()
  await expect(card.getByText('Built reporting pipelines for retail banking clients.')).toBeAttached()
  await expectEvidence(card, 3)
  await expect(card.getByText(REDACTED, { exact: true })).toBeVisible()
  const body = page.locator('body')
  for (const raw of [CV_EMAIL, CV_PHONE, CV_PHONE.replace(' ', '')]) await expect(body).not.toContainText(raw)

  // A new CV version makes the summary stale until it is regenerated.
  copyFileSync(`${data}fixtures/cv-v2/taylor@example.com.docx`, `${data}fixtures/resumes/taylor@example.com.docx`)
  await syncTaylorCv(page, candidateId, 2)
  await page.goto(`/#/candidates/${candidateId}`)
  await page.reload()
  const stale = card.getByText('Source changed since this summary — regenerate')
  await expect(stale).toBeVisible()
  await card.getByRole('button', { name: 'Regenerate' }).click()
  await expect(card.getByRole('button', { name: 'Regenerate' })).toBeEnabled()
  await expect(stale).toHaveCount(0)
  await expectEvidence(card, 3)
  await expect(card.getByText(REDACTED, { exact: true })).toBeVisible()
  for (const raw of [CV_EMAIL, CV_PHONE, CV_PHONE.replace(' ', '')]) await expect(body).not.toContainText(raw)

  // Demand review drawer: the JD keeps only its own sentence.
  await page.goto('/#/demands')
  await page.getByRole('row', { name: /T10001/ }).getByRole('button', { name: 'Review JD' }).click()
  const demandCard = page.getByRole('dialog').getByRole('region', { name: 'Summary' })
  await demandCard.getByRole('button', { name: 'Generate summary' }).click()
  await expectEvidence(demandCard, 1)
  await expect(demandCard.getByText('The role needs SQL and Python.')).toBeVisible()
  await expect(demandCard.locator('blockquote')).toHaveText('SQL, Python')

  // Provider unavailable: Generate gets the backend's 503 and shows the user-facing message.
  writeFileSync(`${data}llm-down`, '')
  try {
    await demandCard.getByRole('button', { name: 'Regenerate' }).click()
    await expect(demandCard.getByRole('alert')).toHaveText('The local AI is unavailable. Try again when it is ready.')
  } finally {
    rmSync(`${data}llm-down`, { force: true })
  }
})
