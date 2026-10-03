export type DashboardDoc = {
  /** Absolute path of the dashboard file. Empty before the first read. */
  path: string
  /** File text, or null when the file does not exist. */
  text: string | null
}

export type DashboardModelView = {
  /** False until the model has seen the dashboard in this context window. */
  isSeen: boolean
  /** The text the model saw last, or null when it saw no file. */
  text: string | null
}

declare module 'claude-code' {
  interface PluginState {
    dashboard: {
      doc: DashboardDoc
      modelView: DashboardModelView
      reminder: number
      dismissed: string[]
    }
  }
}
