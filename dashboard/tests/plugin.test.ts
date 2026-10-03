import { expect, mock, test } from 'claude-code/testing'
import type { On, UiPane } from 'claude-code'
import type { Engine } from 'claude-code/testing'

const ROOT = '/project'
const PATH = `${ROOT}/.claude/local/dashboard.md`

type Fake = {
  files: Map<string, string>
  dirs: Set<string>
  panes: UiPane[]
  cwd: string
}

type FakeOptions = {
  isPlaced?: boolean
  cwd?: string
  dirs?: string[]
  store?: Record<string, unknown>
}

// Answers the engine calls the plugin makes, from memory.
function fake(on: On, files: Record<string, string>, options: FakeOptions | boolean = {}): Fake {
  const {
    isPlaced = true,
    cwd = ROOT,
    dirs = [`${ROOT}/.git`],
    store = {},
  } = typeof options === 'boolean' ? { isPlaced: options } : options
  const state: Fake = { files: new Map(Object.entries(files)), dirs: new Set(dirs), panes: [], cwd }
  let tick = 0
  mock.clock(on)
  mock.store(on, store)
  on('session.root', () => ({ value: ROOT }))
  on('session.cwd', () => ({ value: state.cwd }))
  on('session.id', () => ({ value: 'session-1' }))
  on('fs.exists', ($, e) => ({ value: state.files.has(e.path) || state.dirs.has(e.path) }))
  on('fs.stat', ($, e) => ({
    value: {
      kind: state.dirs.has(e.path) ? ('dir' as const) : ('file' as const),
      size: 0,
      mtimeMs: ++tick,
      isLink: false,
    },
  }))
  on('fs.read', ($, e) => {
    const text = state.files.get(e.path)
    return text === undefined ? { deny: `ENOENT ${e.path}` } : { value: text }
  })
  on('fs.write', ($, e) => {
    state.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('ui.panes', () => ({ value: state.panes }))
  on('ui.open', ($, e) => {
    if (!state.panes.some(pane => pane.id === e.id)) {
      state.panes.push({ id: e.id, title: e.title ?? e.id, isShown: true, isFocused: false, isPlaced })
    }
    return { value: isPlaced ? { isPlaced: true as const } : { isPlaced: false as const, reason: 'narrow' } }
  })
  on('ui.close', ($, e) => {
    state.panes = state.panes.filter(pane => pane.id !== e.id)
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  // The engine's own drawing: the footer's mode labels, and an empty band.
  on('ui.render', ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    return e.component === 'SessionMode'
      ? Box({ children: e.props.modes.map(mode => Text({ children: mode })) })
      : Box({})
  })
  on('prompt.submit', ($, e) => ({ text: e.text, context: e.context }))

  return state
}

// The footer's dashboard label, or undefined when it adds none.
async function footerText($: Engine): Promise<string | undefined> {
  const ui = await $.ui.mount({
    plugin: 'dashboard',
    surface: 'terminal',
    component: 'SessionMode',
    props: { modes: [] },
  })
  const found = await ui.find({ type: 'Text', text: /open dashboard item/ })
  await ui.unmount()

  return found?.text
}

const PANE_PROPS = {
  title: 'Dashboard',
  isFocused: false,
  bodyColumns: 60,
  placement: 'dock' as const,
  scroll: { offset: 0, bodyRows: 20 },
  view: {},
}

test('a pane draws items and a press checks one off in the file', async ($, on) => {
  const state = fake(on, { [PATH]: '# Needs you\n- [ ] Pick a widget\n- [x] Done thing\n' })
  const ran = await $.command.run({
    command: 'dashboard',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 160 },
  })
  expect(ran.text).toBe('Dashboard panel shown')
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'dashboard',
      surface,
      component: 'Pane',
      requestId: 'dashboard',
      props: PANE_PROPS,
    })
    expect((await ui.find({ key: 'item:0:1' }))?.props.label).toBe('☐')
    expect((await ui.find({ key: 'item:0:2' }))?.props.label).toBe('☑')
    await ui.unmount()
  }

  const ui = await $.ui.mount({
    plugin: 'dashboard',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'dashboard',
    props: PANE_PROPS,
  })
  await ui.press({ key: 'item:0:1' })
  expect(state.files.get(PATH)).toBe('# Needs you\n- [x] Pick a widget\n- [x] Done thing\n')
  expect((await ui.find({ key: 'item:0:1' }))?.props.label).toBe('☑')
  expect(await footerText($)).toBeUndefined()
  await ui.unmount()
})

test('the first prompt carries open items; later prompts carry user changes', async ($, on) => {
  const state = fake(on, { [PATH]: '- [ ] Approve the deploy\n' })
  const origin = { kind: 'composer' as const }

  const first = await $.prompt.submit({ text: 'hi', wait: false, origin })
  expect(first.context?.[0]).toContain('Approve the deploy')
  expect(state.panes.map(pane => pane.id)).toEqual(['dashboard'])
  expect(await footerText($)).toBeUndefined()

  const quiet = await $.prompt.submit({ text: 'next', wait: false, origin })
  expect(quiet.context).toBeUndefined()

  state.files.set(PATH, '- [x] Approve the deploy\n')
  const changed = await $.prompt.submit({ text: 'again', wait: false, origin })
  expect(changed.context?.[0]).toContain('Checked off: "Approve the deploy"')
  expect(state.panes).toEqual([])
})

test('a pane the person closed stays closed until a new item appears', async ($, on) => {
  const state = fake(on, { [PATH]: '- [ ] First\n' })
  const origin = { kind: 'composer' as const }

  await $.prompt.submit({ text: 'hi', wait: false, origin })
  expect(state.panes.map(pane => pane.id)).toEqual(['dashboard'])

  const ui = await $.ui.mount({
    plugin: 'dashboard',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'dashboard',
    props: PANE_PROPS,
  })
  await ui.press({ key: 'hide' })
  await ui.unmount()
  expect(state.panes).toEqual([])
  expect(await footerText($)).toBe('1 open dashboard item · /dashboard')
  await $.prompt.submit({ text: 'next', wait: false, origin })
  expect(state.panes).toEqual([])

  state.files.set(PATH, '- [ ] First\n- [ ] Second\n')
  await $.prompt.submit({ text: 'again', wait: false, origin })
  expect(state.panes.map(pane => pane.id)).toEqual(['dashboard'])
})

test('the footer shows the count only while the pane is not drawn', async ($, on) => {
  const state = fake(on, { [PATH]: '- [ ] First\n- [ ] Second\n' }, false)
  const origin = { kind: 'composer' as const }

  await $.prompt.submit({ text: 'hi', wait: false, origin })
  expect(await footerText($)).toBe('2 open dashboard items · /dashboard')

  const pane = state.panes[0]
  if (pane !== undefined) {
    pane.isPlaced = true
  }
  await $.prompt.submit({ text: 'next', wait: false, origin })
  expect(await footerText($)).toBeUndefined()
})

test('/dashboard toggles the pane', async ($, on) => {
  const state = fake(on, { [PATH]: '- [ ] First\n' })
  const run = async (): Promise<string | undefined> =>
    (
      await $.command.run({
        command: 'dashboard',
        args: '',
        origin: { kind: 'composer' },
        presentation: { isFullscreen: true, columns: 160 },
      })
    ).text

  expect(await run()).toBe('Dashboard panel shown')
  expect(state.panes.map(pane => pane.id)).toEqual(['dashboard'])
  expect(await run()).toBe('Dashboard panel hidden')
  expect(state.panes).toEqual([])
  expect(await footerText($)).toBe('1 open dashboard item · /dashboard')
  expect(await run()).toBe('Dashboard panel shown')
  expect(await footerText($)).toBeUndefined()
})

test('the command row draws without the plugin-name prefix', async ($, on) => {
  fake(on, {})
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'dashboard',
      surface,
      component: 'CommandOutput',
      props: { command: 'dashboard', args: '', text: 'dashboard: Dashboard panel shown', isErrored: false },
    })
    expect(await ui.find({ type: 'Text', text: 'Dashboard panel shown' })).toBeDefined()
    await ui.unmount()
  }
})

test('the footer label follows the open count on each surface', async ($, on) => {
  fake(on, { [PATH]: '- [ ] First\n- [ ] Second\n' }, false)
  await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })
  for (const surface of ['terminal', 'desktop'] as const) {
    const footer = await $.ui.mount({
      plugin: 'dashboard',
      surface,
      component: 'SessionMode',
      props: { modes: ['focus'] },
    })
    expect(await footer.find({ type: 'Text', text: 'focus' })).toBeDefined()
    expect(await footer.find({ type: 'Text', text: '2 open dashboard items · /dashboard' })).toBeDefined()
    await footer.unmount()
  }
})

const WORKTREE = `${ROOT}/.claude/worktrees/wt`
const WORKTREE_PATH = `${WORKTREE}/.claude/local/dashboard.md`
const WORKTREE_FILES = {
  [PATH]: '- [ ] Project item\n',
  [WORKTREE_PATH]: '- [ ] Worktree item\n',
  [`${WORKTREE}/.git`]: `gitdir: ${ROOT}/.git/worktrees/wt\n`,
}

async function paneLabels($: Engine): Promise<string[]> {
  const ui = await $.ui.mount({
    plugin: 'dashboard',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'dashboard',
    props: PANE_PROPS,
  })
  const labels = (await ui.findAll({ type: 'Text' }))
    .map(found => found.text ?? '')
    .filter(text => /^(Project|Worktree)/.test(text))
  await ui.unmount()

  return labels
}

test('inside a worktree the pane shows the project and the worktree dashboards', async ($, on) => {
  const state = fake(on, WORKTREE_FILES, { cwd: `${WORKTREE}/src` })
  const first = await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })
  expect(first.context?.length).toBe(2)
  expect(await paneLabels($)).toEqual(['Project', 'Worktree: wt'])
  expect(await footerText($)).toBeUndefined()

  // Leaving the worktree in this process drops its dashboard.
  state.cwd = ROOT
  await $.prompt.submit({ text: 'next', wait: false, origin: { kind: 'composer' } })
  expect(await paneLabels($)).toEqual([])
})

test('after a restart the stored worktree still applies', async ($, on) => {
  // A new process starts in the launch folder, with the worktree in the store.
  fake(on, WORKTREE_FILES, {
    dirs: [`${ROOT}/.git`, WORKTREE],
    store: { worktrees: { 'session-1': { top: WORKTREE, at: 0 } } },
  })
  await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })
  expect(await paneLabels($)).toEqual(['Project', 'Worktree: wt'])
})

test('a worktree dashboard alone draws no heading', async ($, on) => {
  const files: Record<string, string> = { ...WORKTREE_FILES }
  delete files[PATH]
  fake(on, files, { cwd: WORKTREE })
  await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })
  expect(await paneLabels($)).toEqual([])
  expect(await footerText($)).toBeUndefined()
})

test('a stored worktree that no longer exists is ignored', async ($, on) => {
  fake(on, WORKTREE_FILES, { store: { worktrees: { 'session-1': { top: WORKTREE, at: 0 } } } })
  await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })
  expect(await paneLabels($)).toEqual([])
})
