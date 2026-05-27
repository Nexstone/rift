import {Flags} from '@oclif/core'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {GatedCommand} from '../../lib/base-command.js'
import {getDataDir} from '../../lib/python-bridge.js'
import {
  green, red, cyan, bold, dim,
} from '../../lib/tui.js'

export default class PortfolioStop extends GatedCommand {
  static override description = 'Stop the portfolio supervisor and all managed strategies'

  static override examples = [
    '$ rift portfolio stop',
  ]

  async run(): Promise<void> {
    const pidFile = path.join(getDataDir(), 'algo', 'supervisor.pid')

    if (!fs.existsSync(pidFile)) {
      this.log(`\n  ${dim('No portfolio supervisor running.')}\n`)
      return
    }

    let pid: number
    try {
      pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
      process.kill(pid, 0)
    } catch {
      this.log(`\n  ${dim('Supervisor not running (stale PID).')}\n`)
      try { fs.unlinkSync(pidFile) } catch {}
      return
    }

    this.log(`\n  ${dim('Stopping portfolio supervisor')} ${dim(`(PID ${pid})...`)}`)
    this.log(`  ${dim('Closing all positions and saving session logs...')}`)

    // Send SIGTERM
    try {
      process.kill(pid, 'SIGTERM')
    } catch {
      this.log(`  ${dim('Process already exited.')}`)
      return
    }

    // Wait for shutdown (up to 45s — needs time to close all positions)
    for (let i = 0; i < 90; i++) {
      try {
        process.kill(pid, 0)
        await new Promise(r => setTimeout(r, 500))
      } catch {
        break
      }
    }

    // Show final state
    const stateFile = path.join(getDataDir(), 'algo', 'supervisor.json')
    if (fs.existsSync(stateFile)) {
      try {
        const state = JSON.parse(fs.readFileSync(stateFile, 'utf-8'))
        const portfolio = state.portfolio || {}
        const strategies = state.strategies || []

        this.log('')
        this.log(`  ${green('✔')} Portfolio supervisor stopped.`)
        this.log('')
        this.log(`  ${bold('Final State')}`)
        this.log(`  Equity: ${bold('$' + (portfolio.total_equity || 0).toLocaleString())}`)
        this.log(`  Strategies managed: ${strategies.length}`)

        for (const s of strategies) {
          const pnl = s.pnl_pct || 0
          const pnlStr = pnl >= 0 ? green(`+${pnl.toFixed(2)}%`) : red(`${pnl.toFixed(2)}%`)
          this.log(`    ${dim(s.name)} ${dim(s.pair)} — ${pnlStr} (${s.num_trades || 0} trades)`)
        }
      } catch {}
    } else {
      this.log(`\n  ${green('✔')} Portfolio supervisor stopped.\n`)
    }

    // Clean up
    try { fs.unlinkSync(pidFile) } catch {}
    this.log('')
  }
}
