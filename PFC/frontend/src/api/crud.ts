import { ApiError } from './client'

/**
 * Per-item CRUD for OCC lists (TDD.md §12): screens keep a single Save, and the client
 * turns the edited list into POST/PUT/DELETE calls against the server's current list.
 */
export interface ItemChange<T> {
  op: 'create' | 'update' | 'remove'
  item: T
}

export interface CrudOps<T> {
  create(item: T): Promise<unknown>
  update(item: T, before: T): Promise<unknown>
  remove(item: T): Promise<unknown>
}

/** Removals first (so delete-then-add edits succeed), then updates, then creates; order otherwise follows the lists. */
export function diffList<T>(before: T[], after: T[], key: (t: T) => string, same: (a: T, b: T) => boolean): ItemChange<T>[] {
  const old = new Map(before.map((t) => [key(t), t]))
  const next = new Map(after.map((t) => [key(t), t]))
  return [
    ...before.filter((t) => !next.has(key(t))).map((item) => ({ op: 'remove' as const, item })),
    ...after.filter((t) => old.has(key(t)) && !same(old.get(key(t)) as T, t)).map((item) => ({ op: 'update' as const, item })),
    ...after.filter((t) => !old.has(key(t))).map((item) => ({ op: 'create' as const, item })),
  ]
}

/** A save that stopped part-way: earlier changes are kept, `unsaved` lists the failed item and those not attempted. */
export class PartialSaveError extends ApiError {
  readonly unsaved: string[]

  constructor(cause: ApiError, unsaved: string[]) {
    super({ ...cause.problem, detail: `${cause.message} Not saved: ${unsaved.join(', ')}.` })
    this.name = 'PartialSaveError'
    this.unsaved = unsaved
  }
}

/** Applies changes one at a time; the first failure aborts the rest and raises {@link PartialSaveError}. */
export async function applyChanges<T>(
  before: T[],
  after: T[],
  key: (t: T) => string,
  same: (a: T, b: T) => boolean,
  ops: CrudOps<T>,
): Promise<void> {
  const changes = diffList(before, after, key, same)
  const old = new Map(before.map((t) => [key(t), t]))
  for (const [i, c] of changes.entries()) {
    try {
      if (c.op === 'create') await ops.create(c.item)
      else if (c.op === 'update') await ops.update(c.item, old.get(key(c.item)) as T)
      else await ops.remove(c.item)
    } catch (e) {
      if (!(e instanceof ApiError)) throw e
      throw new PartialSaveError(e, changes.slice(i).map((x) => key(x.item)))
    }
  }
}
