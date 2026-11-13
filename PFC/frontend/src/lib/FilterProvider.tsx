import { createContext, useContext, useEffect, useState } from 'react'
import type { Dispatch, ReactNode, SetStateAction } from 'react'
import type { ResultFilters } from '../api/types'

const STORAGE_KEY = 'pfc-result-filters'
const FilterContext = createContext<{
  filters: ResultFilters
  setFilters: Dispatch<SetStateAction<ResultFilters>>
} | null>(null)

export function FilterProvider({ children }: { children: ReactNode }) {
  const [filters, setFilters] = useState<ResultFilters>(() => {
    try {
      const value: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '{}')
      if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
      return Object.fromEntries(Object.entries(value).filter(([key, v]) =>
        key === 'scoreMin' || key === 'scoreMax' ? typeof v === 'number' : Array.isArray(v) && v.every((item) => typeof item === 'string'),
      ))
    } catch {
      return {}
    }
  })
  useEffect(() => {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(filters))
    } catch {
      // Storage may be unavailable in private browsing.
    }
  }, [filters])
  return <FilterContext.Provider value={{ filters, setFilters }}>{children}</FilterContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useResultFilters() {
  const context = useContext(FilterContext)
  if (!context) throw new Error('useResultFilters requires FilterProvider')
  return context
}
