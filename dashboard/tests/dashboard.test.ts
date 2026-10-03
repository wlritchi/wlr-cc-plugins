import { expect, test } from 'claude-code/testing'

import { describeChanges, itemKey, openItems, parse, toggle } from '../hooks/dashboard'

const SAMPLE = [
  '# Dashboard',
  '',
  '## Questions',
  '- [ ] Widget: use the canvas or the DOM renderer?',
  '  The canvas is faster; the DOM is easier to test.',
  '- [x] Keep the legacy API?',
  '',
  '## Tasks for you',
  '* [ ] Run `gh auth login`',
  '',
  '```',
  '- [ ] not an item inside a fence',
  '```',
].join('\n')

test('parse splits prose and items and keeps continuation lines', () => {
  const blocks = parse(SAMPLE)
  const kinds = blocks.map(block => block.kind)
  expect(kinds).toEqual(['markdown', 'item', 'item', 'markdown', 'item', 'markdown'])
  const first = blocks[1]
  expect(first?.kind === 'item' ? first.item.body : '').toBe(
    'Widget: use the canvas or the DOM renderer?\nThe canvas is faster; the DOM is easier to test.',
  )
  expect(first?.kind === 'item' ? first.item.line : -1).toBe(3)
})

test('openItems ignores checked items and fenced code', () => {
  expect(openItems(SAMPLE).map(itemKey)).toEqual([
    'Widget: use the canvas or the DOM renderer?',
    'Run `gh auth login`',
  ])
  expect(openItems(null)).toEqual([])
})

test('toggle flips one box and refuses a stale line', () => {
  const checked = toggle(SAMPLE, 3, false)
  expect(checked?.split('\n')[3]).toBe('- [x] Widget: use the canvas or the DOM renderer?')
  expect(toggle(SAMPLE, 3, true)).toBeUndefined()
  expect(toggle(SAMPLE, 0, false)).toBeUndefined()
  expect(toggle(SAMPLE, 99, false)).toBeUndefined()
})

test('describeChanges names checked, added, removed and prose changes', () => {
  const after = (toggle(SAMPLE, 3, false) ?? '')
    .replace('* [ ] Run `gh auth login`', '- [ ] Approve the deploy')
    .replace('# Dashboard', '# Dashboard (updated)')
  expect(describeChanges(SAMPLE, after)).toEqual([
    'Checked off: "Widget: use the canvas or the DOM renderer?"',
    'Added: "Approve the deploy"',
    'Removed: "Run `gh auth login`"',
    'Text outside the checklist items changed.',
  ])
  expect(describeChanges(SAMPLE, SAMPLE)).toEqual([])
  expect(describeChanges(SAMPLE, null)).toEqual(['The dashboard file was deleted.'])
})
