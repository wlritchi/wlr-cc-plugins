// Parsing and editing of the dashboard file. No engine calls here, so the
// tests can call these functions directly.

export type Item = {
  /** Zero-based line index of the checkbox line. */
  line: number
  indent: number
  isDone: boolean
  /** The checkbox line's text after the box, with continuation lines. */
  body: string
}

export type Block = { kind: 'markdown'; text: string } | { kind: 'item'; item: Item }

const ITEM = /^(\s*)[-*+]\s+\[([ xX])\](?:\s+(.*))?$/
const FENCE = /^\s*(```|~~~)/

const indentOf = (line: string): number => line.length - line.trimStart().length

/** Splits the file into prose runs and checklist items, in file order. */
export function parse(text: string | null): Block[] {
  if (text === null) {
    return []
  }
  const lines = text.split('\n')
  const blocks: Block[] = []
  let prose: string[] = []
  let isInFence = false

  const flushProse = (): void => {
    const joined = prose.join('\n').trim()
    if (joined !== '') {
      blocks.push({ kind: 'markdown', text: joined })
    }
    prose = []
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? ''
    if (FENCE.test(line)) {
      isInFence = !isInFence
    }
    const match = isInFence ? null : ITEM.exec(line)
    if (match === null) {
      prose.push(line)
      continue
    }
    flushProse()
    const start = i
    const indent = (match[1] ?? '').length
    const body = [match[3] ?? '']
    // Indented non-item lines below a checkbox belong to that item.
    for (let next = lines[i + 1]; next !== undefined; next = lines[i + 1]) {
      if (next.trim() === '' || indentOf(next) <= indent || ITEM.test(next)) {
        break
      }
      body.push(next.trim())
      i++
    }
    blocks.push({
      kind: 'item',
      item: { line: start, indent, isDone: match[2] !== ' ', body: body.join('\n') },
    })
  }
  flushProse()

  return blocks
}

export function items(text: string | null): Item[] {
  return parse(text).flatMap(block => (block.kind === 'item' ? [block.item] : []))
}

export function openItems(text: string | null): Item[] {
  return items(text).filter(item => !item.isDone)
}

/** A stable key for an item: its first line, whitespace collapsed. */
export function itemKey(item: Item): string {
  return (item.body.split('\n')[0] ?? '').replace(/\s+/g, ' ').trim()
}

/**
 * Flips the checkbox on `line`. Returns undefined when that line is no longer
 * a checkbox in the state `wasDone`, for example after an edit from another
 * writer.
 */
export function toggle(text: string, line: number, wasDone: boolean): string | undefined {
  const lines = text.split('\n')
  const current = lines[line]
  const match = current === undefined ? null : ITEM.exec(current)
  if (current === undefined || match === null || (match[2] !== ' ') !== wasDone) {
    return undefined
  }
  lines[line] = current.replace(/\[[ xX]\]/, wasDone ? '[ ]' : '[x]')

  return lines.join('\n')
}

const quote = (item: Item): string => `"${itemKey(item)}"`

/** Lists the item-level changes from `before` to `after`, one line each. */
export function describeChanges(before: string | null, after: string | null): string[] {
  if (after === null) {
    return before === null ? [] : ['The dashboard file was deleted.']
  }
  const old = new Map(items(before).map(item => [itemKey(item), item]))
  const now = new Map(items(after).map(item => [itemKey(item), item]))
  const changes: string[] = []

  for (const [key, item] of now) {
    const was = old.get(key)
    if (was === undefined) {
      changes.push(`Added${item.isDone ? ' (already checked)' : ''}: ${quote(item)}`)
    } else if (was.isDone !== item.isDone) {
      changes.push(`${item.isDone ? 'Checked off' : 'Unchecked'}: ${quote(item)}`)
    } else if (was.body !== item.body) {
      changes.push(`Edited: ${quote(item)}`)
    }
  }
  for (const [key, item] of old) {
    if (!now.has(key)) {
      changes.push(`Removed: ${quote(item)}`)
    }
  }
  const prose = (text: string | null): string =>
    parse(text)
      .flatMap(block => (block.kind === 'markdown' ? [block.text] : []))
      .join('\n')
  if (prose(before) !== prose(after)) {
    changes.push('Text outside the checklist items changed.')
  }

  return changes
}

/** Resolves `.` and `..` segments of an absolute POSIX path. */
export function normalize(path: string): string {
  const parts: string[] = []
  for (const part of path.split('/')) {
    if (part === '..') {
      parts.pop()
    } else if (part !== '' && part !== '.') {
      parts.push(part)
    }
  }

  return `/${parts.join('/')}`
}

/**
 * The main checkout's top folder for a linked worktree, from the text of the
 * worktree's `.git` file. Undefined for any other `.git` file (a submodule).
 */
export function mainTopFromGitFile(top: string, gitFile: string): string | undefined {
  const gitdir = /^gitdir:\s*(.+?)\s*$/m.exec(gitFile)?.[1]
  if (gitdir === undefined) {
    return undefined
  }
  const absolute = normalize(gitdir.startsWith('/') ? gitdir : `${top}/${gitdir}`)
  const cut = absolute.lastIndexOf('/.git/worktrees/')

  return cut < 0 ? undefined : absolute.slice(0, cut) || '/'
}

/** True when `path` is `dir` or is inside it. */
export const isInside = (path: string, dir: string): boolean =>
  path === dir || path.startsWith(dir.endsWith('/') ? dir : `${dir}/`)
