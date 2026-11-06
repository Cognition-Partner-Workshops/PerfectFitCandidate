/** Placeholder palette for the email template (GUI_OCC_DESIGN.md §15, REQ-EML-03). */
export const EMAIL_PLACEHOLDERS = [
  '{candidate_name}',
  '{requisition}',
  '{role}',
  '{match_pct}',
  '{gaps}',
  '{pointers}',
  '{spoc_1}',
  '{spoc_2}',
]

/** Placeholder-like tokens in a template that are not in the palette. */
export function unknownPlaceholders(template: string): string[] {
  const found = template.match(/\{[a-z0-9_]+\}/gi) ?? []
  return [...new Set(found.filter((p) => !EMAIL_PLACEHOLDERS.includes(p)))]
}
