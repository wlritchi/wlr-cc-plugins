import { expect, mock, test } from 'claude-code/testing'
import type { On, UiPane } from 'claude-code'
import type { Engine } from 'claude-code/testing'

const ROOT = '/project'
const PATH = `${ROOT}/.claude/local/dashboard.md`

type Fake = {
  files: Map<string, string>
  panes: UiPane[]
}

// Answers the engine calls the plugin makes, from memory.
function fake(on: On, files: Record<string, string>, isPlaced = true): Fake {
  const state: Fake = { files: new Map(Object.entries(files)), panes: [] }
  let tick = 0
  mock.clock(on)
  on('session.root', () => ({ value: ROOT }))
  on('fs.exists', ($, e) => ({ value: state.files.has(e.path) }))
  on('fs.stat', () => ({ value: { kind: 'file' as const, size: 0, mtimeMs: ++tick, isLink: false } }))
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

// The band's count text, or undefined when the band draws nothing of its own.
async function bandText($: Engine): Promise<string | undefined> {
  const ui = await $.ui.mount({
    plugin: 'dashboard',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: {
      hasSurvey: false,
      isWorking: false,
      maxRows: 10,
      bodyColumns: 80,
      scroll: { offset: 0, bodyRows: 10 },
      view: {},
    },
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
    expect((await ui.find({ key: 'item:1' }))?.props.label).toBe('☐')
    expect((await ui.find({ key: 'item:2' }))?.props.label).toBe('☑')
    await ui.unmount()
  }

  const ui = await $.ui.mount({
    plugin: 'dashboard',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'dashboard',
    props: PANE_PROPS,
  })
  await ui.press({ key: 'item:1' })
  expect(state.files.get(PATH)).toBe('# Needs you\n- [x] Pick a widget\n- [x] Done thing\n')
  expect((await ui.find({ key: 'item:1' }))?.props.label).toBe('☑')
  expect(await bandText($)).toBeUndefined()
  await ui.unmount()
})

test('the first prompt carries open items; later prompts carry user changes', async ($, on) => {
  const state = fake(on, { [PATH]: '- [ ] Approve the deploy\n' })
  const origin = { kind: 'composer' as const }

  const first = await $.prompt.submit({ text: 'hi', wait: false, origin })
  expect(first.context?.[0]).toContain('Approve the deploy')
  expect(state.panes.map(pane => pane.id)).toEqual(['dashboard'])
  expect(await bandText($)).toBeUndefined()

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
  await $.prompt.submit({ text: 'next', wait: false, origin })
  expect(state.panes).toEqual([])

  state.files.set(PATH, '- [ ] First\n- [ ] Second\n')
  await $.prompt.submit({ text: 'again', wait: false, origin })
  expect(state.panes.map(pane => pane.id)).toEqual(['dashboard'])
})

test('the band shows the count only while the pane is not drawn', async ($, on) => {
  const state = fake(on, { [PATH]: '- [ ] First\n- [ ] Second\n' }, false)
  const origin = { kind: 'composer' as const }

  await $.prompt.submit({ text: 'hi', wait: false, origin })
  expect(await bandText($)).toBe('2 open dashboard items')

  const pane = state.panes[0]
  if (pane !== undefined) {
    pane.isPlaced = true
  }
  await $.prompt.submit({ text: 'next', wait: false, origin })
  expect(await bandText($)).toBeUndefined()
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
  expect(await bandText($)).toBe('1 open dashboard item')
  expect(await run()).toBe('Dashboard panel shown')
  expect(await bandText($)).toBeUndefined()
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

test('the footer label and the band Button follow the open count', async ($, on) => {
  const state = fake(on, { [PATH]: '- [ ] First\n- [ ] Second\n' }, false)
  await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })

  for (const surface of ['terminal', 'desktop'] as const) {
    const footer = await $.ui.mount({
      plugin: 'dashboard',
      surface,
      component: 'SessionMode',
      props: { modes: ['focus'] },
    })
    expect(await footer.find({ type: 'Text', text: /2 open dashboard items · \/dashboard/ })).toBeDefined()
    await footer.unmount()
  }

  const pane = state.panes[0]
  if (pane !== undefined) {
    pane.isPlaced = true
  }
  const band = await $.ui.mount({
    plugin: 'dashboard',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: {
      hasSurvey: false,
      isWorking: false,
      maxRows: 10,
      bodyColumns: 80,
      scroll: { offset: 0, bodyRows: 10 },
      view: {},
    },
  })
  await band.press({ key: 'show' })
  await band.unmount()
  expect(await bandText($)).toBeUndefined()
})
