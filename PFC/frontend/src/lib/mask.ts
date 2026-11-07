/**
 * Presentation masking (REQ-OUT-03): keep the first 2 characters of the local part and replace each
 * remaining local-part character with one `*`, keeping `@` and the domain, e.g.
 * `AlexBorder@capgemini.com` → `Al********@capgemini.com`. A local part of 2 characters or fewer is
 * fully masked, and a value without `@` is masked as a local part.
 * Masking is a presentation control, not encryption.
 */
export function maskEmail(email: string | null | undefined, keep = 2): string | null {
  if (!email) return null
  const at = email.indexOf('@')
  const local = at >= 0 ? email.slice(0, at) : email
  const domain = at >= 0 ? email.slice(at) : ''
  const chars = Array.from(local)
  const shown = chars.length > keep ? keep : 0
  return `${chars.slice(0, shown).join('')}${'*'.repeat(chars.length - shown)}${domain}`
}

/** Phone masking: keep the country prefix and the last 2 digits, e.g. `+44 7700 900189` → `+44 ******89`. */
export function maskPhone(phone: string | null | undefined): string | null {
  if (!phone) return null
  const compact = phone.replace(/\s+/g, '')
  const prefix = compact.startsWith('+') ? compact.slice(0, 3) + ' ' : ''
  return `${prefix}******${compact.slice(-2)}`
}
