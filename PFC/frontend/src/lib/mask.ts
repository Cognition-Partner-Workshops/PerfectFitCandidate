/**
 * Presentation masking (REQ-OUT-03): keep the first 2 characters, mask up to 6 following characters,
 * then the remainder of the local part, e.g. `AlexBorder@capgemini.com` → `Al******er@capgemini.com`.
 * Masking is a presentation control, not encryption.
 */
export function maskEmail(email: string | null | undefined, keep = 2, masked = 6): string | null {
  if (!email) return null
  const at = email.indexOf('@')
  const local = at >= 0 ? email.slice(0, at) : email
  const domain = at >= 0 ? email.slice(at) : ''
  const chars = Array.from(local)
  if (chars.length <= keep) return `${'*'.repeat(chars.length)}${domain}`
  const shown = chars.slice(0, keep).join('')
  const rest = chars.slice(keep + masked).join('')
  return `${shown}${'*'.repeat(Math.min(masked, chars.length - keep))}${rest}${domain}`
}

/** Phone masking: keep the country prefix and the last 2 digits, e.g. `+44 7700 900189` → `+44 ******89`. */
export function maskPhone(phone: string | null | undefined): string | null {
  if (!phone) return null
  const compact = phone.replace(/\s+/g, '')
  const prefix = compact.startsWith('+') ? compact.slice(0, 3) + ' ' : ''
  return `${prefix}******${compact.slice(-2)}`
}
