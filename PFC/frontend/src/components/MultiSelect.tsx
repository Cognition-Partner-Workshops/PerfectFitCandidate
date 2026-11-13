import { useEffect, useId, useRef, useState } from 'react'

export function MultiSelect({ label, value, options, onChange, render = (v) => v }: {
  label: string
  value?: string[]
  options: string[]
  onChange: (value: string[] | undefined) => void
  render?: (value: string) => string
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const all = useRef<HTMLInputElement>(null)
  const id = useId()
  const selected = value ?? []
  useEffect(() => {
    if (!open) return
    all.current?.focus()
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  return (
    <div className="multi-select" ref={root} onKeyDown={(event) => {
      if (event.key === 'Escape') {
        setOpen(false)
        button.current?.focus()
      }
    }} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}>
      <span className="multi-select-label">{label}</span>
      <button ref={button} type="button" className="btn" aria-label={label} aria-expanded={open}
        aria-controls={id} onClick={() => setOpen((v) => !v)}>
        {!selected.length ? 'All' : selected.length === 1 ? render(selected[0]) : `${selected.length} selected`}
      </button>
      {open && <div id={id} className="multi-select-popover" role="group" aria-label={`${label} options`}>
        <label><input ref={all} type="checkbox" checked={!selected.length} onChange={() => onChange(undefined)} />All</label>
        {[...new Set([...options, ...selected])].map((option) => (
          <label key={option}><input type="checkbox" checked={selected.includes(option)} onChange={() => {
            const next = selected.includes(option) ? selected.filter((v) => v !== option) : [...selected, option]
            onChange(next.length ? next : undefined)
          }} />{render(option)}</label>
        ))}
      </div>}
    </div>
  )
}
