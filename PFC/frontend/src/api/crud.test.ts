import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { applyChanges, diffList, PartialSaveError } from './crud'

type Row = { k: string; v: number }
const key = (r: Row) => r.k
const same = (a: Row, b: Row) => a.v === b.v
const conflict = () => new ApiError({ type: 'about:blank', title: 'Conflict', status: 409, code: 'CONFLICT', detail: 'Status is referenced by status_map' })

describe('per-item CRUD diff (TDD.md §12)', () => {
  it('orders removals, then updates, then creates, and skips unchanged rows', () => {
    const before = [{ k: 'a', v: 1 }, { k: 'b', v: 1 }, { k: 'c', v: 1 }]
    const after = [{ k: 'd', v: 1 }, { k: 'b', v: 2 }, { k: 'c', v: 1 }]
    expect(diffList(before, after, key, same).map((c) => `${c.op}:${c.item.k}`)).toEqual(['remove:a', 'update:b', 'create:d'])
  })

  it('stops at the first failure and lists the failed and unattempted items', async () => {
    const ops = { create: vi.fn(async () => undefined), update: vi.fn(async () => undefined), remove: vi.fn(async () => { throw conflict() }) }
    const err = await applyChanges([{ k: 'a', v: 1 }], [{ k: 'b', v: 1 }], key, same, ops).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(PartialSaveError)
    expect((err as PartialSaveError).unsaved).toEqual(['a', 'b'])
    expect((err as PartialSaveError).problem.code).toBe('CONFLICT')
    expect((err as Error).message).toBe('Status is referenced by status_map Not saved: a, b.')
    expect(ops.create).not.toHaveBeenCalled()
  })

  it('rethrows non-API errors unchanged', async () => {
    const boom = new TypeError('x')
    const ops = { create: async () => { throw boom }, update: async () => undefined, remove: async () => undefined }
    await expect(applyChanges([], [{ k: 'a', v: 1 }], key, same, ops)).rejects.toBe(boom)
  })
})
