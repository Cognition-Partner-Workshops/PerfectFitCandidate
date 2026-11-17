import { useId } from 'react'

const FORMATS = [
  ['.docx', 'Word (.docx)'],
  ['.pdf', 'PDF (.pdf)'],
  ['.txt', 'Text (.txt)'],
  ['.pptx', 'PowerPoint (.pptx)'],
  ['.odt', 'OpenDocument (.odt)'],
  ['.rtf', 'Rich text (.rtf)'],
  ['.doc', 'Word 97-2003 (.doc)'],
  ['.ppt', 'PowerPoint 97-2003 (.ppt)'],
] as const

export function CvFileFormats({ value, converterAvailable, onChange }: {
  value: string[]
  converterAvailable: boolean
  onChange: (value: string[]) => void
}) {
  const id = useId()
  const invalid = value.length === 0
  return (
    <fieldset className="stack" aria-describedby={`${id}-help${!converterAvailable ? ` ${id}-converter` : ''}${invalid ? ` ${id}-error` : ''}`} aria-invalid={invalid}>
      <legend>CV file formats in this folder</legend>
      {FORMATS.map(([extension, label]) => {
        const checked = value.includes(extension)
        const legacy = extension === '.doc' || extension === '.ppt'
        return (
          <label key={extension} className="row small">
            <input
              type="checkbox"
              checked={checked}
              disabled={legacy && !converterAvailable && !checked}
              onChange={(event) => onChange(event.target.checked ? [...value, extension] : value.filter((item) => item !== extension))}
            />
            {label}
          </label>
        )
      })}
      {!converterAvailable && <span id={`${id}-converter`} className="small muted">Converter not installed — save as .docx/.pptx</span>}
      <span id={`${id}-help`} className="small muted">Unticking a format does not delete CVs already synced.</span>
      {invalid && <span id={`${id}-error`} className="error-text" role="alert">Select at least one CV file format.</span>}
    </fieldset>
  )
}
