export type DashboardDoc = {
  /** Absolute path of the dashboard file. */
  path: string
  /** The pane's heading for this dashboard when it shows more than one. */
  label: string
  /** File text, or null when the file does not exist. */
  text: string | null
}

export type DashboardWorktree = {
  /** Top folder of the linked worktree the session works in, or null. */
  top: string | null
  /** True when this process saw the session in `top`, not only the store. */
  isObserved: boolean
  /** True once the value was loaded from the store in this process. */
  isLoaded: boolean
}

declare module 'claude-code' {
  interface PluginState {
    dashboard: {
      docs: DashboardDoc[]
      /** Per dashboard path, the text the model saw last in this context window. */
      seen: Record<string, string | null>
      worktree: DashboardWorktree
      reminder: number
      dismissed: string[]
    }
  }
}
