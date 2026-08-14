/**
 * Git decoration model for file trees and change lists.
 *
 * Mirrors how editors decorate a workspace: every changed path gets a colour +
 * a one-letter badge, and every ANCESTOR DIRECTORY inherits the decoration of
 * its most significant descendant so a collapsed folder still signals that
 * something inside it changed.
 *
 * The backend sends raw Git porcelain letters; mapping them to a semantic kind
 * happens here, once, so the tree, the changes list and any future view stay
 * visually consistent.
 */

export type GitDecorationKind =
  | 'untracked'
  | 'added'
  | 'modified'
  | 'renamed'
  | 'deleted'
  | 'conflict'

export interface GitDecoration {
  kind: GitDecorationKind
  /** One-letter badge shown at the end of the row. */
  letter: string
  /** True when the decoration comes from a descendant (directory rollup). */
  inherited: boolean
  /** True when the change is in the index. Undefined for inherited entries. */
  staged?: boolean
}

/** Raw porcelain letter → semantic kind. */
const KIND_BY_STATUS: Record<string, GitDecorationKind> = {
  '?': 'untracked',
  A: 'added',
  C: 'added',
  M: 'modified',
  T: 'modified',
  R: 'renamed',
  D: 'deleted',
  U: 'conflict',
}

/** Badge letter per kind. Untracked reads as "U"ntracked, conflict as "!". */
const LETTER_BY_KIND: Record<GitDecorationKind, string> = {
  untracked: 'U',
  added: 'A',
  modified: 'M',
  renamed: 'R',
  deleted: 'D',
  conflict: '!',
}

/**
 * Which decoration wins when a directory contains several kinds of change,
 * and when one path is reported twice (staged + dirty). Higher wins.
 */
const SEVERITY: Record<GitDecorationKind, number> = {
  conflict: 5,
  deleted: 4,
  modified: 3,
  renamed: 2,
  added: 1,
  untracked: 0,
}

export interface GitChangeLike {
  path: string
  status: string
  staged?: boolean
}

/** Semantic kind of a raw porcelain status letter, or null when unknown. */
export function kindOfStatus(status: string): GitDecorationKind | null {
  return KIND_BY_STATUS[status] ?? null
}

/** Badge letter for a raw porcelain status letter. */
export function letterOfStatus(status: string): string {
  const kind = kindOfStatus(status)
  return kind ? LETTER_BY_KIND[kind] : '?'
}

function makeDecoration(
  kind: GitDecorationKind,
  inherited: boolean,
  staged?: boolean
): GitDecoration {
  return { kind, letter: LETTER_BY_KIND[kind], inherited, staged }
}

/** Keep whichever decoration is more significant. */
function upsert(
  map: Record<string, GitDecoration>,
  path: string,
  next: GitDecoration
): void {
  const current = map[path]
  if (!current || SEVERITY[next.kind] > SEVERITY[current.kind]) {
    map[path] = next
  }
}

/**
 * Build a `path → decoration` lookup for a flat list of changes.
 *
 * Ancestor directories are filled in during the same pass, which keeps lookups
 * in the tree O(1) instead of scanning the change list per node.
 */
export function buildGitDecorations(
  changes: GitChangeLike[]
): Record<string, GitDecoration> {
  const map: Record<string, GitDecoration> = {}

  for (const change of changes) {
    const kind = kindOfStatus(change.status)
    if (!kind || !change.path) continue

    upsert(map, change.path, makeDecoration(kind, false, change.staged))

    // Roll the decoration up through every ancestor directory.
    const segments = change.path.split('/')
    for (let i = 1; i < segments.length; i += 1) {
      upsert(map, segments.slice(0, i).join('/'), makeDecoration(kind, true))
    }
  }

  return map
}

/** File name of a path, tolerating a trailing slash. */
export function baseName(path: string): string {
  const trimmed = (path || '').replace(/\/+$/, '')
  if (!trimmed) return path || ''
  return trimmed.slice(trimmed.lastIndexOf('/') + 1)
}

/** Parent directory of a path ('' when the path is at the root). */
export function dirName(path: string): string {
  const trimmed = (path || '').replace(/\/+$/, '')
  const index = trimmed.lastIndexOf('/')
  return index === -1 ? '' : trimmed.slice(0, index)
}
