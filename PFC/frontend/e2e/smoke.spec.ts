import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

/**
 * End-to-end smoke test against the real backend (REQ-AUTH-01/02, REQ-ING-01..03, REQ-OUT-03).
 * `e2e/serve.py` starts FastAPI on a fresh data dir with synthetic DL/CT workbooks.
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

  const signIn = page.getByRole('form', { name: 'Sign in' })
  await signIn.getByRole('combobox').selectOption('Admin')
  await signIn.locator('input[type="email"]').fill(EMAIL)
  await signIn.locator('#login-password').fill(PASSWORD)
  await signIn.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()

  await page.goto('/#/demands')
  await expect(page.getByRole('heading', { name: 'Demand List' })).toBeVisible()
  await upload(page, 'Upload DL', 'Demand List workbook', 'dl.xlsx')
  await expect(page.getByText('T10001')).toBeVisible()

  await page.goto('/#/bench')
  await expect(page.getByRole('heading', { name: 'Bench List' })).toBeVisible()
  await upload(page, 'Upload CT', 'Candidate Tracker workbook', 'ct.xlsx')
  await expect(page.getByText('SmokeAlex')).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Eligible (1)' })).toBeVisible()
  // C1 is not in the seeded grade list, so the second synthetic row is excluded from matching.
  await page.getByRole('tab', { name: 'Excluded from matching (1)' }).click()
  await expect(page.getByText('SmokeTaylor')).toBeVisible()
})
