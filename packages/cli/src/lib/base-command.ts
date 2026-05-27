/**
 * Base command for RIFT — every CLI command extends `GatedCommand`.
 *
 * The historical name "GatedCommand" is kept to avoid churning 17 import
 * sites; it doesn't actually gate anything in v0.1. Acts as the central
 * point for cross-cutting CLI behavior:
 *
 *   Persistent status footer (Phase 0 polish item #5)
 *     - Renders a single-line footer at the end of every command's output
 *       showing system state (live ready / research-only / kill switch / etc.)
 *     - TTY-only — suppressed for piped output so `rift list-data | jq`
 *       still produces clean JSON
 *     - Opt out per command by setting `static skipFooter = true`
 *       (e.g., `home` which renders its own bottom-of-screen status)
 *
 * Future hook points (Phase 1+):
 *   - Fee-gating prompts (where the name originally came from)
 *   - Telemetry emit
 *   - Auth state checks before live commands
 */

import {Command} from '@oclif/core'

import {printStatusFooterIfTTYWithSpacing} from './status-footer.js'

export abstract class GatedCommand extends Command {
  /**
   * Subclass override: skip the auto-rendered status footer.
   * For commands that render their own bottom-of-screen status (home)
   * or emit raw structured data where a trailing footer would be wrong.
   */
  static skipFooter = false

  async init(): Promise<void> {
    await super.init()
  }

  /**
   * oclif lifecycle: called after run() succeeds AND after catch() on error.
   * Footer renders regardless of outcome — TTY check protects piped output.
   */
  protected async finally(err: Error | undefined): Promise<unknown> {
    const ctor = this.constructor as typeof GatedCommand
    if (!ctor.skipFooter) {
      try {
        printStatusFooterIfTTYWithSpacing()
      } catch {
        // Never let footer rendering break command execution
      }
    }
    return super.finally(err)
  }
}
