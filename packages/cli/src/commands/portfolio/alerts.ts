import {GatedCommand} from '../../lib/base-command.js'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {getDataDir} from '../../lib/python-bridge.js'
import {
  green, red, yellow, cyan, bold, dim,
} from '../../lib/tui.js'

export default class PortfolioAlerts extends GatedCommand {
  static override description = 'Show recent portfolio alerts'

  static override examples = [
    '$ rift portfolio alerts',
  ]

  async run(): Promise<void> {
    const alertsFile = path.join(getDataDir(), 'algo', 'alerts.log')

    if (!fs.existsSync(alertsFile)) {
      this.log(`\n  ${dim('No alerts yet.')}\n`)
      return
    }

    const lines = fs.readFileSync(alertsFile, 'utf-8').trim().split('\n').filter(l => l.trim())

    if (lines.length === 0) {
      this.log(`\n  ${dim('No alerts yet.')}\n`)
      return
    }

    this.log('')
    this.log(`  ${bold('Recent Alerts')} ${dim(`(${lines.length} total)`)}`)
    this.log(`  ${dim('─'.repeat(60))}`)
    this.log('')

    // Show last 20
    const recent = lines.slice(-20)
    for (const line of recent) {
      try {
        const alert = JSON.parse(line)
        const event = (alert.event || '').padEnd(18)
        const eventColor = event.includes('trade') ? cyan
          : event.includes('health') ? yellow
          : event.includes('drawdown') ? red
          : event.includes('session_died') ? red
          : event.includes('schedule') ? dim
          : event.includes('risk') ? yellow
          : dim

        this.log(`  ${dim(alert.time || '??:??')}  ${eventColor(event)} ${alert.message || ''}`)
      } catch {
        // Skip unparseable lines
      }
    }

    this.log('')
  }
}
