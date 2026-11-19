import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
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
  const setup = await (await page.request.get('/api/v1/setup/status')).json() as { admin_exists: boolean }
  if (!setup.admin_exists) {
    await expect(page.getByRole('heading', { name: 'Create the OCC administrator account' })).toBeVisible()
    await page.getByLabel('Setup code').fill(readFileSync(`${data}run/setup-code.txt`, 'utf8').trim())
    await page.getByLabel('Full name').fill('Test Owner')
    await page.getByLabel('Corporate email').fill(EMAIL)
    await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
    await page.getByLabel('Confirm password').fill(PASSWORD)
    await page.getByRole('button', { name: 'Create admin' }).click()
    await page.getByRole('button', { name: 'Next' }).click()
    await page.getByRole('button', { name: 'Go to sign in' }).click()
  }

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
  await expect(page.getByText('SQL, Python', { exact: true })).toBeVisible()
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

  await result.getByRole('link', { name: 'SmokeAlex', exact: true }).click()
  await expect(page).toHaveURL(/\/candidates\/\d+$/)
  await expect(page.getByRole('tab', { name: 'Matches & score history' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /SmokeAlex/ })).toBeVisible()
  await expect(page.getByText('Something went wrong in the PFC service', { exact: false })).toHaveCount(0)
  await page.getByRole('tab', { name: 'Matches & score history' }).click()
  await expect(page.getByRole('row').filter({ has: page.getByRole('link', { name: 'T10001', exact: true }) })).toBeVisible()
  await expect(page.locator('body')).not.toContainText('alex@example.com')
  await page.getByRole('link', { name: 'T10001', exact: true }).click()
  await result.getByRole('button', { name: 'Evidence and gaps' }).click()
  const evidence = page.getByRole('heading', { name: 'Matching evidence', exact: true }).locator('..').locator('ul').first().getByRole('listitem')
  await expect.poll(() => evidence.count()).toBeGreaterThanOrEqual(2)
  for (const item of await evidence.all()) {
    await expect(item).toBeVisible()
    await expect(item).not.toContainText('; ')
  }

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
})

test('follow-up settings and Outlook draft calendar handoff (REQ-EML-08)', async ({ page }) => {
  await page.goto('/')
  await signIn(page)
  await page.goto('/#/occ/admin-settings')
  await page.getByLabel('Enable Outlook follow-up reminder').check()
  await page.getByLabel('Reminder time').fill('10:31')
  await page.getByLabel('Reminder time').fill('10:30')
  await page.getByRole('combobox', { name: 'Email transport', exact: true }).selectOption('eml_handoff')
  await page.getByRole('button', { name: 'Save admin settings' }).click()
  await expect(page.getByText('Admin settings saved.')).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('Reminder time')).toHaveValue('10:30')

  // Reuse the draft on a retry rather than re-creating the administrator or re-sending mail.
  const auth = await (await page.request.get('/api/v1/auth/me')).json()
  let emails = (await (await page.request.get('/api/v1/emails')).json()).drafts
  if (!emails.length) {
    const runs = await (await page.request.get('/api/v1/match-runs')).json()
    const created = await page.request.post('/api/v1/emails/drafts', {
      headers: { 'X-CSRF-Token': auth.csrf_token }, data: { run_id: runs[0].runId },
    })
    expect(created.ok()).toBeTruthy()
    emails = (await created.json()).drafts
  }
  expect(emails.length).toBeGreaterThan(0)
  const email = emails[0]
  await page.goto('/#/email')
  await page.reload()
  if (email.status === 'Draft') {
    await page.getByRole('button', { name: /^Approve email to / }).click()
  }
  if (email.status !== 'Sent') {
    await page.getByRole('tab', { name: /^Approved \(/ }).click()
    if (!email.handoff_at) {
      const draftDownload = page.waitForEvent('download')
      await page.getByRole('button', { name: 'Open in Outlook', exact: true }).click()
      expect((await draftDownload).suggestedFilename()).toMatch(/\.eml$/)
    }
    await page.getByRole('button', { name: 'Mark as sent', exact: true }).click()
  }
  await page.getByRole('tab', { name: /^Sent \(/ }).click()
  const reminder = page.getByRole('listitem').filter({ has: page.getByRole('button', { name: 'Add to Outlook calendar' }) }).first()
  await expect(reminder.getByText(/^Follow-up /)).toContainText('10:30')
  const download = page.waitForEvent('download')
  await reminder.getByRole('button', { name: 'Add to Outlook calendar' }).click()
  const calendar = await download
  expect(await calendar.failure()).toBeNull()
  expect(calendar.suggestedFilename()).toMatch(/\.ics$/)
  const calendarPath = await calendar.path()
  const ics = readFileSync(calendarPath!, 'utf8')
  expect(ics).toContain('BEGIN:VEVENT\r\n')
  expect(ics).toContain('TRIGGER:PT0M\r\n')
  expect(ics).toContain('TRANSP:TRANSPARENT\r\n')
})

test('chat answers offline with source records and keeps history (P2-EX-05)', async ({ page }) => {
  // Reuse the admin and demands set up by the first smoke test.
  await page.goto('/')
  await signIn(page)
  await page.getByRole('link', { name: 'Chat', exact: true }).click()
  await expect(page.getByText('Local model ready')).toBeVisible()

  const question = 'How many open demands are there?'
  await page.getByRole('button', { name: question }).click()
  await expect(page.getByRole('textbox', { name: 'Question' })).toHaveValue(question)
  await page.getByRole('button', { name: 'Ask', exact: true }).click()
  await expect(page.getByText('Count of open demands: 2.')).toBeVisible()
  const sources = page.getByRole('region', { name: 'Sources (2)' })
  await expect(sources.getByRole('link', { name: 'T10001' })).toBeVisible()
  await expect(sources.getByRole('link', { name: 'T10002' })).toBeVisible()
  const task = page.getByRole('link', { name: 'Chat question' })
  await expect(task).toHaveAttribute('href', /#\/chat\?job=\d+$/)

  await sources.getByRole('link', { name: 'T10001' }).click()
  await expect(page.getByRole('heading', { name: 'Demand List' })).toBeVisible()
  await expect(page.getByRole('row', { name: /T10001/ })).toBeVisible()
  await expect(page.getByRole('row', { name: /T10002/ })).toHaveCount(0)
  await task.click()
  await page.reload()
  await expect(page.getByText('Count of open demands: 2.')).toBeVisible()
  await expect(page.getByRole('button', { name: /How many open demands/ }))
    .toHaveAttribute('aria-current', 'true')
})

// REQ-LLM-02 summary cards. Runs after the first smoke test, on its admin, DL/CT and confirmed JD.
// Uses SmokeTaylor (C1, listed under All), who has no match results.
// `serve.py` injects the backend's FakeProvider with one summary reply for every
// source: the backend keeps only the sentences whose evidence is in that source.
const LABEL = 'AI summary for reading only — not used in scoring. Check against the source.'
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
  const contactSentence = card.getByText(/^Reach Taylor at /)
  const contactEvidence = card.locator('blockquote').filter({ hasText: /^Contact: / })
  const expectRedactedContact = async () => {
    await expect(contactSentence).toBeVisible()
    await expect(contactSentence).toHaveText('Reach Taylor at [email removed] or [phone removed].')
    await expect(contactEvidence).toBeVisible()
    await expect(contactEvidence).toHaveText('Contact: [email removed]; phone: [phone removed].')
  }
  await expectRedactedContact()
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

  // CV version comparison (REQ-IN-24 / FR-ING-52): read-only, side by side, PII masked.
  await page.getByRole('tab', { name: /CV versions/ }).click()
  const boxes = page.getByRole('checkbox', { name: /for comparison$/ })
  await expect(boxes).toHaveCount(2)
  await boxes.nth(0).check()
  await boxes.nth(1).check()
  await page.getByRole('button', { name: 'Compare selected (2/2)' }).click()
  const comparison = page.getByRole('region', { name: 'CV version comparison' })
  await expect(comparison.getByText('Read-only', { exact: true })).toBeVisible()
  const textDiff = comparison.getByRole('region', { name: 'Text differences' })
  await expect(textDiff.locator('td.diff-new').filter({ hasText: 'Led a move to cloud warehouses.' })).toBeVisible()
  await expect(comparison.getByRole('region', { name: 'Score differences' })).toBeVisible()
  for (const raw of [CV_EMAIL, CV_PHONE, CV_PHONE.replace(' ', '')]) await expect(body).not.toContainText(raw)
  const shots = process.env.PFC_SCREENSHOT_DIR
  if (shots) {
    await page.setViewportSize({ width: 1400, height: 3600 })
    await comparison.screenshot({ path: `${shots}/cv-compare.png` })
    const score = comparison.getByRole('button', { name: 'Score both versions' })
    if (await score.isEnabled()) {
      await score.click()
      await expect(comparison.getByRole('table', { name: 'What-if scores' })).toBeVisible({ timeout: 30_000 })
      await comparison.getByText('Score breakdown').first().click()
      await comparison.getByRole('region', { name: 'Score differences' }).screenshot({ path: `${shots}/cv-compare-scores.png` })
    }
    await page.setViewportSize({ width: 1280, height: 720 })
  }
  await comparison.getByRole('button', { name: 'Close comparison' }).click()
  await expect(comparison).toBeHidden()
  await expect(stale).toHaveCount(0)
  await expectEvidence(card, 3)
  await expectRedactedContact()
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

test('lifecycle end/reopen, exact archive selection, masked archive search and CSV export', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1100 })
  const retry = testInfo.retry
  const requisition = `T19${retry}01`
  const resource = `DemoMorgan${retry}`
  const screenshots = process.env.PFC_SCREEN_DIR
  const capture = async (name: string) => {
    if (screenshots) {
      const minimize = page.getByRole('button', { name: 'Minimize tasks' })
      if (await page.getByRole('dialog').count() === 0 && await minimize.isVisible()) await minimize.click()
      mkdirSync(screenshots, { recursive: true })
      await page.screenshot({ path: `${screenshots}/${name}.png`, fullPage: true })
    }
  }
  await page.goto('/')
  await signIn(page)
  await page.goto('/#/demands')
  await upload(page, 'Upload DL', 'Demand List workbook', `dl-lifecycle-${retry}.xlsx`)
  const demandRow = page.getByRole('row', { name: new RegExp(requisition) })
  const demandTable = page.getByRole('table').first()
  await expect.poll(() => demandTable.evaluate((table) => table.scrollWidth <= table.parentElement!.clientWidth + 1)).toBe(true)
  await capture('demand-list-compact')
  await demandRow.getByRole('button', { name: 'Lifecycle', exact: true }).click()
  await demandRow.getByRole('button', { name: 'End of cycle…' }).click()
  const end = page.getByRole('dialog', { name: 'End of cycle…' })
  await expect(end.getByLabel('Reason (required)').locator('option')).not.toHaveCount(1)
  await end.getByLabel('Reason (required)').selectOption({ index: 1 })
  await end.getByLabel(/^Comment/).fill('Meridian demo engagement completed; approved for archival.')
  await capture('end-of-cycle-dialog')
  await end.getByRole('button', { name: 'End cycle' }).click()
  await expect(demandRow).toBeHidden()

  await page.goto('/#/bench')
  await upload(page, 'Upload CT', 'Candidate Tracker workbook', `ct-lifecycle-${retry}.xlsx`)
  await page.getByRole('tab', { name: /^All \(/ }).click()
  const resourceRow = page.getByRole('row', { name: new RegExp(resource) })
  await expect.poll(() => page.getByRole('table').first().evaluate((table) => table.scrollWidth <= table.parentElement!.clientWidth + 1)).toBe(true)
  await capture('bench-list-compact')
  await resourceRow.getByRole('button', { name: 'Lifecycle', exact: true }).click()
  await resourceRow.getByRole('button', { name: 'End of cycle…' }).click()
  await expect(end.getByLabel('Reason (required)').locator('option')).not.toHaveCount(1)
  await end.getByLabel('Reason (required)').selectOption('assigned')
  await end.getByLabel(/^Comment/).fill('Demo resource engagement completed.')
  await end.getByRole('button', { name: 'End cycle' }).click()
  await expect(resourceRow).toBeHidden()
  await page.getByRole('button', { name: 'Ended resources' }).click()
  const endedResource = page.locator('.row').filter({ has: page.locator('strong', { hasText: resource }) })
  await endedResource.getByRole('button', { name: 'Lifecycle history', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Lifecycle history' })).toContainText('Off bench')
  await page.getByRole('region', { name: 'Lifecycle history' }).scrollIntoViewIfNeeded()
  await capture('lifecycle-history')
  await endedResource.getByRole('button', { name: 'Reopen', exact: true }).click()
  const reopen = page.getByRole('dialog', { name: 'Reopen record' })
  await expect(reopen.getByLabel('Reason (required)').locator('option')).not.toHaveCount(1)
  await reopen.getByLabel('Reason (required)').selectOption({ index: 1 })
  await reopen.getByLabel(/^Comment/).fill('Demo resource available for a new engagement.')
  await reopen.getByRole('button', { name: 'Reopen', exact: true }).click()
  await expect(resourceRow).toBeVisible()

  await page.goto('/#/occ/archival')
  await page.getByRole('button', { name: 'Build archive preview' }).click()
  const preview = page.getByRole('region', { name: 'Archive preview' })
  await expect(preview.getByRole('checkbox', { name: `Include ${requisition}`, exact: true })).toBeVisible()
  await expect(preview.getByRole('row', { name: new RegExp(requisition) })).toContainText('Filled — manual')
  for (const checkbox of await preview.getByRole('checkbox').all()) {
    if (await checkbox.getAttribute('aria-label') !== `Include ${requisition}`) await checkbox.uncheck()
  }
  await preview.getByRole('tab', { name: /^Resources/ }).click()
  for (const checkbox of await preview.getByRole('checkbox').all()) await checkbox.uncheck()
  await preview.getByRole('tab', { name: /^Demands/ }).click()
  await capture('occ-archival-preview')
  await page.getByRole('region', { name: 'Housekeeping', exact: true }).scrollIntoViewIfNeeded()
  await capture('housekeeping')
  await preview.getByRole('button', { name: 'Archive 1 record' }).click()
  const confirm = page.getByRole('dialog', { name: 'Confirm archive' })
  await expect(confirm).toContainText('Archive exactly 1 demand and 0 resources')
  await confirm.getByRole('button', { name: 'Confirm archive' }).click()
  await expect(preview.getByText('Archive completed. 1 demand and 0 resources processed.')).toBeVisible()
  const showTasks = page.getByRole('button', { name: /^Show tasks/ })
  if (await showTasks.isVisible()) await showTasks.click()
  await expect(page.getByRole('region', { name: 'Task status' })).toContainText('Archive records')

  await page.getByRole('link', { name: 'Archive', exact: true }).click()
  await page.getByLabel('Search archive').fill(requisition)
  await expect(page.getByRole('table', { name: 'Archived records' })).toContainText(requisition)
  await capture('archive-page')
  await page.getByRole('button', { name: 'Read-only detail' }).click()
  await expect(page.getByRole('dialog')).toContainText('Read-only')
  await expect(page.getByRole('region', { name: 'Lifecycle history' })).toContainText('Archived')
  await page.getByRole('button', { name: 'Close', exact: true }).click()
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export CSV' }).click()
  const csv = await downloaded
  expect(csv.suggestedFilename()).toBe('archived-demands.csv')
  expect(readFileSync((await csv.path())!, 'utf8')).toContain(requisition)
})

test('watch pickup: automatic DL task card and held CT review as an analyst', async ({ page }, info) => {
  test.setTimeout(240_000)
  await page.setViewportSize({ width: 1440, height: 1600 })
  // Also works with --grep watch on a fresh server, and on retries after setup already exists.
  const setup = await (await page.request.get('/api/v1/setup/status')).json()
  if (!setup.admin_exists) {
    const created = await page.request.post('/api/v1/setup/admin', { data: { setup_code: readFileSync(`${data}run/setup-code.txt`, 'utf8').trim(), email: EMAIL, display_name: 'Test Owner', password: PASSWORD } })
    expect(created.status()).toBe(201)
  }
  const login = await page.request.post('/api/v1/auth/login', { data: { role: 'Admin', email: EMAIL, password: PASSWORD } })
  expect(login.ok()).toBe(true)
  const adminCsrf = (await login.json()).csrf_token
  const email = `watch-${Date.now()}@example.com`
  const created = await page.request.post('/api/v1/occ/users', { headers: { 'X-CSRF-Token': adminCsrf }, data: { email, display_name: 'Watch Analyst', role: 'Analyst', persona: 'A' } })
  expect(created.status()).toBe(201)
  const otp = (await created.json()).one_time_password
  expect((await page.request.post('/api/v1/auth/logout', { headers: { 'X-CSRF-Token': adminCsrf } })).ok()).toBe(true)
  const analyst = await page.request.post('/api/v1/auth/login', { data: { role: 'Analyst', email, password: otp } })
  expect(analyst.ok()).toBe(true)
  const csrf = (await analyst.json()).csrf_token
  const password = 'Watch-Analyst-Passw0rd!'
  expect((await page.request.post('/api/v1/auth/password', { headers: { 'X-CSRF-Token': csrf }, data: { current_password: otp, new_password: password } })).ok()).toBe(true)
  await page.context().clearCookies()
  await page.goto('/')
  const form = page.getByRole('form', { name: 'Sign in' })
  await form.getByRole('combobox').selectOption('Analyst')
  await form.locator('input[type="email"]').fill(email)
  await form.locator('#login-password').fill(password)
  await form.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
  await page.goto('/#/settings/data')
  const dl = page.getByRole('group', { name: 'Auto-sync — Demand List' })
  const ct = page.getByRole('group', { name: 'Auto-sync — Candidate Tracker' })
  const dlName = `DL_${info.retry}.xlsx`
  const ctName = `CT_${info.retry}.xlsx`
  rmSync(`${data}watch/${dlName}`, { force: true })
  rmSync(`${data}watch/${ctName}`, { force: true })
  await dl.getByLabel('Enabled', { exact: true }).check()
  await dl.getByLabel('Watch path').fill(`${data}watch`)
  await dl.getByLabel('Poll every N minutes').fill('1')
  await dl.getByLabel('Hold for my review before committing').uncheck()
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Saved', { exact: true })).toBeVisible()
  copyFileSync(`${data}fixtures/dl-watch-${info.retry}.xlsx`, `${data}watch/${dlName}`)
  const tasks = page.getByRole('region', { name: 'Task status' })
  await expect(tasks.getByRole('link', { name: `Auto-pickup Demand List: ${dlName} — committed: Completed`, exact: true })).toBeVisible({ timeout: 95_000 })
  const shots = join(homedir(), 'pfc-watch-shots')
  mkdirSync(shots, { recursive: true })
  await tasks.screenshot({ path: join(shots, 'task-card.png') })
  await expect(dl.getByText('Folder reachable', { exact: true })).toBeVisible()
  await expect(dl.getByText(new RegExp(`Last pickup: ${dlName} — committed`))).toBeVisible({ timeout: 20_000 })
  await page.goto('/#/demands')
  await expect(page.getByRole('row', { name: new RegExp(`T19${String(info.retry).padStart(3, '0')}`) })).toBeVisible()
  await page.goto('/#/settings/data')
  await ct.getByLabel('Enabled', { exact: true }).check()
  await ct.getByLabel('Watch path').fill(`${data}watch`)
  await ct.getByLabel('Poll every N minutes').fill('1')
  await ct.getByLabel('Hold for my review before committing').check()
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Saved', { exact: true })).toBeVisible()
  copyFileSync(`${data}fixtures/ct-watch-${info.retry}.xlsx`, `${data}watch/${ctName}`)
  await expect(ct.getByRole('button', { name: 'Review', exact: true })).toBeVisible({ timeout: 95_000 })
  await ct.getByRole('button', { name: 'Review', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Review Candidate Tracker — preview' })
  await expect(dialog.getByText('Rows read 1', { exact: true })).toBeVisible()
  await page.screenshot({ path: join(shots, 'review-dialog.png'), fullPage: true })
  await dialog.getByRole('button', { name: 'Commit 1 rows', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(ct.getByText('No files awaiting review.', { exact: true })).toBeVisible()
  await expect(ct.getByText(new RegExp(`Last pickup: ${ctName} — committed`))).toBeVisible()
  await dl.scrollIntoViewIfNeeded()
  await page.screenshot({ path: join(shots, 'settings-data.png'), fullPage: true })
})
