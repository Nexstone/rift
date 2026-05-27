/**
 * Phase 0 persistent status footer.
 *
 * Renders a one-line footer at the bottom of every interactive CLI command:
 *
 *   ──────────────────────────────────────────────────────────────────────
 *   ● live ready  ·  agent 0x9b14…3a7c  ·  mainnet  ·  rift v0.1.0
 *
 * Suppressed when stdout is piped (process.stdout.isTTY === false) — same
 * pattern git uses for colors. Avoids polluting `rift list-data | jq` output.
 *
 * Color/indicator legend:
 *   ● green   → live ready (mainnet)
 *   ○ gray    → research-only
 *   ✗ red     → broken (kill switch, agent revoked, etc.)
 *   ⚠ yellow  → incomplete setup
 */

import {SetupState, SetupStatus, getSetupStatus, shortAddr} from './setup-status.js'

// ANSI color helpers (same style as home.ts already uses)
const GREEN  = '\x1b[32m'
const GRAY   = '\x1b[2m\x1b[37m'
const RED    = '\x1b[31m'
const YELLOW = '\x1b[33m'
const DIM    = '\x1b[2m'
const RESET  = '\x1b[0m'

const HORIZONTAL_RULE = '─'.repeat(70)

interface IndicatorLine {
  indicator: string
  label: string
  detail: string
}

function buildIndicator(s: SetupStatus): IndicatorLine {
  switch (s.state) {
    case 'live-ready':
      return {
        indicator: `${GREEN}●${RESET}`,
        label: 'live ready',
        detail: `agent ${shortAddr(s.agentAddress)}  ·  mainnet  ·  rift v${s.riftVersion}`,
      }
    case 'research-only':
      return {
        indicator: `${GRAY}○${RESET}`,
        label: 'research-only',
        detail: `run \`rift init\` to enable trading  ·  rift v${s.riftVersion}`,
      }
    case 'incomplete':
      return {
        indicator: `${YELLOW}⚠${RESET}`,
        label: `${YELLOW}setup incomplete${RESET}`,
        detail: `run \`rift\` to continue  ·  rift v${s.riftVersion}`,
      }
    case 'kill-active':
      return {
        indicator: `${RED}✗${RESET}`,
        label: `${RED}KILL SWITCH ACTIVE${RESET}`,
        detail: `delete ~/.rift/KILL to resume  ·  rift v${s.riftVersion}`,
      }
    case 'agent-revoked':
      return {
        indicator: `${RED}✗${RESET}`,
        label: `${RED}agent revoked${RESET}`,
        detail: `run \`rift agent-rotate\`  ·  rift v${s.riftVersion}`,
      }
    case 'fresh':
    default:
      return {
        indicator: `${GRAY}○${RESET}`,
        label: 'not initialized',
        detail: `run \`rift init\` to get started  ·  rift v${s.riftVersion}`,
      }
  }
}

/**
 * Render the footer as a multi-line string. Caller writes it via console.log
 * (stdout) — but only when stdout is a TTY.
 */
export function renderStatusFooter(s: SetupStatus = getSetupStatus()): string {
  const {indicator, label, detail} = buildIndicator(s)
  return [
    `${DIM}${HORIZONTAL_RULE}${RESET}`,
    `${indicator} ${label}  ${DIM}·${RESET}  ${DIM}${detail}${RESET}`,
  ].join('\n')
}

/**
 * Print the footer to stdout IFF stdout is a TTY.
 * Use this from oclif command run() methods at the end of each command.
 *
 * Calling this when stdout is piped (e.g. `rift list-data | jq`) is a no-op.
 */
export function printStatusFooterIfTTY(s?: SetupStatus): void {
  if (!process.stdout.isTTY) return
  // eslint-disable-next-line no-console
  console.log(renderStatusFooter(s))
}

/**
 * Same as printStatusFooterIfTTY but adds a leading blank line so the
 * footer is visually separated from preceding output.
 */
export function printStatusFooterIfTTYWithSpacing(s?: SetupStatus): void {
  if (!process.stdout.isTTY) return
  // eslint-disable-next-line no-console
  console.log('')
  console.log(renderStatusFooter(s))
}
