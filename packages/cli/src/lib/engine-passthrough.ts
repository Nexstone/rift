/**
 * Thin passthrough helper for TS wrappers around Python engine commands.
 *
 * Each engine command emits NDJSON over stdout. This helper:
 *   - mirrors `progress` / `status` messages as human-readable lines,
 *   - surfaces `error` messages via the caller's `error()` (which exits),
 *   - pretty-prints any `result` payload as JSON.
 *
 * Wrappers that need to render rich tables/colors should NOT use this —
 * see cost.ts for the per-command rendering pattern.
 */

import {runEngine} from './python-bridge.js'
import type {EngineMessage} from './python-bridge.js'

const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

const red = (s: string) => `\x1b[31m${s}\x1b[0m`

export interface PassthroughOptions {
  command: string
  args: string[]
  /** Bound to the command's this.log */
  log: (msg: string) => void
  /**
   * Bound to the command's this.error — must exit with a non-zero code.
   * Only invoked for genuine engine crashes (non-zero exit with no
   * structured error message already surfaced).
   */
  error: (msg: string) => never
  /** Bound to the command's this.exit — silent non-zero exit. */
  exit: (code: number) => never
  /** When true, only emit the final result JSON (no progress/status output) */
  jsonOnly?: boolean
}

export async function passthroughToEngine(opts: PassthroughOptions): Promise<void> {
  // Track whether the engine surfaced its own structured error message.
  // If it did, we suppress the generic "Engine exited with code N" footer
  // on the non-zero exit — the user already saw the helpful message.
  let surfacedError = false

  try {
    await runEngine(opts.command, opts.args, (msg: EngineMessage) => {
      const type = msg.type as string
      if (type === 'error' && msg.msg) {
        // Log + flag instead of opts.error() — throwing CLIError from an
        // async readline callback isn't caught by oclif's lifecycle and
        // the message gets swallowed silently. Let the engine's non-zero
        // exit propagate via runEngine's rejection path; the catch below
        // exits silently when an error has already been surfaced.
        if (!opts.jsonOnly) opts.log(`  ${red('Error:')} ${msg.msg}`)
        surfacedError = true
        return
      }
      if (opts.jsonOnly && type !== 'result') return
      if (type === 'progress' && msg.msg) {
        opts.log(dim(`  ${msg.msg}`))
      } else if (type === 'status' && msg.msg) {
        opts.log(`  ${msg.msg}`)
      } else if (type === 'result') {
        // Drop the type field; emit the rest as pretty JSON.
        const {type: _t, ...rest} = msg
        opts.log(JSON.stringify(rest, null, 2))
      } else if (msg.msg) {
        opts.log(`  ${msg.msg}`)
      }
    })
  } catch (err) {
    if (surfacedError) {
      opts.exit(1)
    }
    // Engine crashed without emitting a structured error message — re-raise
    // via opts.error so the user sees the stderr-extracted message.
    opts.error(err instanceof Error ? err.message : String(err))
  }
}
