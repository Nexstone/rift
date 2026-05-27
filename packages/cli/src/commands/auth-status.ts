/**
 * `rift auth-status` — show current API wallet registration + recent token activity.
 *
 * Thin TS wrapper around the Python `agent-status` command (which inspects
 * ~/.rift/credentials and ~/.rift/tokens/ via rift_trade modules and emits
 * structured NDJSON). Renders a human-readable summary; suppresses the
 * persistent footer because this command IS the status display.
 */

import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'

// Same ANSI helpers used elsewhere in the codebase
const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const amber = (s: string) => `\x1b[33m${s}\x1b[0m`
const gray  = (s: string) => `\x1b[2m\x1b[37m${s}\x1b[0m`
const red   = (s: string) => `\x1b[31m${s}\x1b[0m`
const bold  = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim   = (s: string) => `\x1b[2m${s}\x1b[0m`
const cyan  = (s: string) => `\x1b[36m${s}\x1b[0m`

function shortAddr(addr: string | null | undefined): string {
  if (!addr) return '—'
  if (addr.length < 14) return addr
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`
}

interface AgentStatusResult {
  registered: boolean
  msg?: string
  agent?: {
    address: string
    network: 'mainnet'
    name: string
    registered_at: string
    registered_tx: string | null
  }
  tokens?: {
    total: number
    active: number
    recent: Array<{
      id: string
      issuer: string
      issued_at: string
      expires_at: string | null
      revoked: boolean
      valid: boolean
      scope_coins: string | string[]
      scope_max_notional: string
      scope_max_daily: string
    }>
  }
}

export default class AuthStatus extends GatedCommand {
  static override description = 'Show RIFT auth state — API wallet + recent authorization tokens.'

  static override examples = [
    '$ rift auth-status',
  ]

  async run(): Promise<void> {
    let result: AgentStatusResult | null = null

    try {
      await runEngine('agent-status', [], (msg: EngineMessage) => {
        if (msg.type === 'result' && msg.command === 'agent-status') {
          result = msg as unknown as AgentStatusResult
        }
      })
    } catch (err) {
      this.error(`Failed to read agent status: ${err}`)
    }

    if (!result) {
      this.error('No response from engine')
    }

    // TypeScript inference doesn't narrow through async callback assignment.
    const r = result as unknown as AgentStatusResult

    this.log('')
    this.log(`  ${bold('RIFT auth status')}`)
    this.log(`  ${dim('─'.repeat(50))}`)
    this.log('')

    if (!r.registered) {
      this.log(`  ${gray('○')} ${bold('No API wallet registered.')}`)
      this.log('')
      this.log(`  ${dim('To enable trading:')}`)
      this.log(`    ${cyan('rift agent-pair --local-main-key <0x...>')}`)
      this.log('')
      this.log(`  ${dim('Or stay in research-only mode (no wallet, no live trading):')}`)
      this.log(`    ${cyan('rift backtest btc_funding_fade --pair BTC')}`)
      this.log('')
      return
    }

    const agent = r.agent!
    const tokens = r.tokens!
    const networkLabel = green('mainnet')

    this.log(`  ${green('●')} ${bold('API wallet registered')} on ${networkLabel}`)
    this.log('')
    this.log(`  ${dim('Address:')}      ${cyan(agent.address)}`)
    this.log(`  ${dim('Name (HL):')}    ${agent.name}`)
    this.log(`  ${dim('Registered:')}   ${agent.registered_at}`)
    if (agent.registered_tx) {
      this.log(`  ${dim('Tx hash:')}      ${dim(agent.registered_tx)}`)
    }

    // Pull live HL state — mode + tradeable collateral — using the shared
    // helper that mirrors Python's read_collateral. Best-effort: if the
    // info endpoint is unreachable (e.g. geo-restricted) we skip silently.
    try {
      const {readCollateral, hlBaseUrl} = await import('../lib/account-mode.js')
      // Main wallet address (the issuer) is what HL queries by. For an
      // API-wallet-only context we'd need the main wallet; here we use
      // the most-recent token's issuer if available, otherwise skip.
      const mainAddr = (r.tokens?.recent?.[0]?.issuer) || null
      if (mainAddr) {
        const c = await readCollateral(hlBaseUrl(), mainAddr)
        const modeColor = c.mode === 'unknown' ? amber : c.mode === 'standard' ? green : cyan
        this.log(`  ${dim('Account mode:')} ${modeColor(c.mode)}`)
        if (c.mode === 'standard') {
          this.log(`  ${dim('Tradeable:')}    $${c.total.toFixed(2)} USDC`)
        } else {
          this.log(`  ${dim('Tradeable:')}    $${c.total.toFixed(2)} USDC  ${dim(`(perp $${c.perpAvailable.toFixed(2)} + spot $${c.spotUsdc.toFixed(2)})`)}`)
        }
      }
    } catch {
      // Best-effort; don't fail the whole status command if HL is unreachable
    }
    this.log('')

    this.log(`  ${bold(`Auth tokens (${tokens.active} active / ${tokens.total} total)`)}`)
    if (tokens.recent.length === 0) {
      this.log(`  ${gray('No tokens issued. Issue one: rift token-issue --coins ETH --max-notional 500 --max-daily 2000')}`)
    } else {
      this.log(`  ${dim('id')}            ${dim('coins')}       ${dim('max/trade')}  ${dim('max/day')}   ${dim('expires')}`)
      for (const t of tokens.recent) {
        const status = t.revoked
          ? red('revoked')
          : t.valid
          ? green('valid')
          : amber('expired')
        const coins = Array.isArray(t.scope_coins) ? t.scope_coins.join(',') : t.scope_coins
        const expires = t.expires_at ? t.expires_at.slice(0, 19).replace('T', ' ') : dim('never')
        this.log(`  ${t.id.slice(0, 8)}…  ${coins.padEnd(11)} $${t.scope_max_notional.padEnd(10)} $${t.scope_max_daily.padEnd(9)}  ${expires}  ${status}`)
      }
    }
    this.log('')
  }
}
