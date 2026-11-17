import { afterEach, describe, expect, it, vi } from 'vitest'
import { HttpApiClient } from './http'

const row = {
  resume_root_path: '/cvs',
  watch: { dl: { enabled: false, path: null, pattern: 'DL_*.xlsx' }, ct: { enabled: false, path: null, pattern: 'CT_*.xlsx' } },
  active_model_id: null,
  density: 'comfortable',
  resumeFileTypes: ['.odt', '.rtf'],
  resume_converter_available: false,
}

describe('resume settings mapping', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('maps backend formats and converter availability and PUTs only editable fields', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async () => new Response(JSON.stringify(row), { headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetch)
    const api = new HttpApiClient('/api/v1')
    const settings = await api.getMySettings()
    expect(settings.resumeFileTypes).toEqual(['.odt', '.rtf'])
    expect(settings.resumeConverterAvailable).toBe(false)
    await api.saveMySettings({ ...settings, resumeFileTypes: ['.txt'] })
    const body = JSON.parse((fetch.mock.calls[1][1] as RequestInit).body as string)
    expect(body.resumeFileTypes).toEqual(['.txt'])
    expect(body).not.toHaveProperty('resumeConverterAvailable')
    expect(body).not.toHaveProperty('resume_converter_available')
  })

  it('maps an available converter and safely defaults fields absent on an older server', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...row, resume_converter_available: true })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...row, resumeFileTypes: undefined, resume_converter_available: undefined })))
    vi.stubGlobal('fetch', fetch)
    const api = new HttpApiClient('/api/v1')
    expect((await api.getMySettings()).resumeConverterAvailable).toBe(true)
    const settings = await api.getMySettings()
    expect(settings.resumeFileTypes).toEqual(['.docx', '.pdf'])
    expect(settings.resumeConverterAvailable).toBe(false)
  })
})
