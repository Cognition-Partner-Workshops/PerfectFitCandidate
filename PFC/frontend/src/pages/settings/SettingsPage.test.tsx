import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, mock, renderApp, seed, signIn } from '../../test/utils'

describe('CV folder formats', () => {
  beforeEach(async () => { await seed(); await signIn('Admin', ADMIN.email, ADMIN.password) })
  afterEach(() => vi.restoreAllMocks())

  it('renders all formats, defaults to docx/pdf, saves selections and discards edits', async () => {
    const save = vi.spyOn(mock, 'saveMySettings')
    renderApp('/settings/data')
    const word = await screen.findByRole('checkbox', { name: 'Word (.docx)' }) as HTMLInputElement
    expect(screen.getByRole('group', { name: 'CV file formats in this folder' })).toBeTruthy()
    expect(word.checked).toBe(true)
    expect((screen.getByRole('checkbox', { name: 'PDF (.pdf)' }) as HTMLInputElement).checked).toBe(true)
    for (const name of ['Text (.txt)', 'PowerPoint (.pptx)', 'OpenDocument (.odt)', 'Rich text (.rtf)', 'Word 97-2003 (.doc)', 'PowerPoint 97-2003 (.ppt)']) {
      const checkbox = screen.getByRole('checkbox', { name }) as HTMLInputElement
      expect(checkbox.checked).toBe(false)
      expect(checkbox.disabled).toBe(false)
    }
    expect(screen.getByText('Unticking a format does not delete CVs already synced.')).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Text (.txt)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ resumeFileTypes: ['.docx', '.pdf', '.txt'] })))
    await screen.findByText('Saved')
    fireEvent.click(word)
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(word.checked).toBe(true)
  })

  it('shows an inline error and prevents saving no formats until one is selected', async () => {
    const save = vi.spyOn(mock, 'saveMySettings')
    renderApp('/settings/data')
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Word (.docx)' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'PDF (.pdf)' }))
    expect(screen.getByRole('alert').textContent).toBe('Select at least one CV file format.')
    const button = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Rich text (.rtf)' }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(button.disabled).toBe(false)
  })

  it('disables ticking unavailable legacy formats but allows unticking saved selections', async () => {
    const settings = await mock.getMySettings()
    vi.spyOn(mock, 'getMySettings').mockResolvedValue({ ...settings, resumeFileTypes: ['.docx', '.doc'], resumeConverterAvailable: false })
    renderApp('/settings/data')
    const legacyWord = await screen.findByRole('checkbox', { name: 'Word 97-2003 (.doc)' }) as HTMLInputElement
    const legacySlides = screen.getByRole('checkbox', { name: 'PowerPoint 97-2003 (.ppt)' }) as HTMLInputElement
    expect(screen.getByText('Converter not installed — save as .docx/.pptx')).toBeTruthy()
    expect(legacyWord.checked).toBe(true)
    expect(legacyWord.disabled).toBe(false)
    expect(legacySlides.disabled).toBe(true)
    fireEvent.click(legacyWord)
    expect(legacyWord.checked).toBe(false)
    expect(legacyWord.disabled).toBe(true)
    expect((screen.getByRole('checkbox', { name: 'Word (.docx)' }) as HTMLInputElement).disabled).toBe(false)
  })

  it('shows save errors without losing the format selection', async () => {
    vi.spyOn(mock, 'saveMySettings').mockRejectedValue(new Error('Settings unavailable'))
    renderApp('/settings/data')
    const text = await screen.findByRole('checkbox', { name: 'Text (.txt)' }) as HTMLInputElement
    fireEvent.click(text)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Settings unavailable')).toBeTruthy()
    expect(text.checked).toBe(true)
  })
})
