import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { MultiSelect } from './MultiSelect'

function Example({ options = ['One', 'Two'] }: { options?: string[] }) {
  const [value, setValue] = useState<string[]>()
  return <><MultiSelect label="Example" options={options} value={value} onChange={setValue} /><button>Outside</button></>
}

describe('MultiSelect', () => {
  it('opens from the keyboard, selects values, and closes with Escape restoring focus', async () => {
    const user = userEvent.setup()
    render(<Example />)
    const button = screen.getByRole('button', { name: 'Example' })
    button.focus()
    await user.keyboard('{Enter}')
    expect((screen.getByRole('checkbox', { name: 'All' }) as HTMLInputElement).checked).toBe(true)
    await user.click(screen.getByRole('checkbox', { name: 'One' }))
    expect(button.textContent).toBe('One')
    await user.click(screen.getByRole('checkbox', { name: 'Two' }))
    expect(button.textContent).toBe('2 selected')
    expect((screen.getByRole('checkbox', { name: 'All' }) as HTMLInputElement).checked).toBe(false)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('group')).toBeNull()
    expect(document.activeElement).toBe(button)
    await user.click(button)
    await user.click(screen.getByRole('checkbox', { name: 'All' }))
    expect(button.textContent).toBe('All')
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }))
    expect(screen.queryByRole('group')).toBeNull()
  })

  it('offers All even with no options and closes when focus leaves', async () => {
    const user = userEvent.setup()
    render(<Example options={[]} />)
    await user.click(screen.getByRole('button', { name: 'Example' }))
    expect(screen.getAllByRole('checkbox').length).toBe(1)
    await user.tab()
    expect(screen.queryByRole('group')).toBeNull()
  })
})
