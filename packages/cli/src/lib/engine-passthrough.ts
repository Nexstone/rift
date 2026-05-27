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

export interface PassthroughOptions {
  command: string
  args: string[]
  /** Bound to the command's this.log */
  log: (msg: string) => void
  /** Bound to the command's this.error — must exit */
  error: (msg: string) => never
  /** When true, only emit the final result JSON (no progress/status output) */
  jsonOnly?: boolean
}

export async function passthroughToEngine(opts: PassthroughOptions): Promise<void> {
  await runEngine(opts.command, opts.args, (msg: EngineMessage) => {
    const type = msg.type as string
    if (type === 'error' && msg.msg) {
      opts.error(msg.msg as string)
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
}
