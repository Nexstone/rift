import {GatedCommand} from '../../lib/base-command.js'
import {runEngine} from '../../lib/python-bridge.js'
import type {EngineMessage} from '../../lib/python-bridge.js'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const yellow = (s: string) => `\x1b[33m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

export default class CollectStatus extends GatedCommand {
  static override description = 'Check data collector status and statistics'

  static override examples = [
    '$ rift collect status',
  ]

  async run(): Promise<void> {
    this.log('')
    this.log(`  ${bold('Data Collector Status')}`)
    this.log(`  ${dim('─'.repeat(45))}`)
    this.log('')

    await runEngine('collect-status', [], (msg: EngineMessage) => {
      if (msg.type === 'result') {
        const running = msg.running as boolean
        const dbExists = msg.db_exists as boolean
        const dbSize = msg.db_size_mb as number
        const obCount = msg.orderbook_count as number
        const marketCount = msg.market_count as number
        const oldest = msg.oldest_data as string | null
        const newest = msg.newest_data as string | null
        const symbols = msg.symbols as string[]

        if (running) {
          this.log(`  ${green('●')} Collector is ${green('running')} (PID ${msg.pid})`)
        } else {
          this.log(`  ${red('●')} Collector is ${red('stopped')}`)
          this.log(`    ${dim('Start with: rift collect start')}`)
        }

        this.log('')

        if (dbExists) {
          this.log(`  ${bold('Database:')} ${dim(`~/.rift/collector.db (${dbSize} MB)`)}`)
          this.log(`  ${bold('Order book snapshots:')} ${obCount.toLocaleString()}`)
          this.log(`  ${bold('Market snapshots:')} ${marketCount.toLocaleString()}`)

          if (symbols && symbols.length > 0) {
            this.log(`  ${bold('Symbols:')} ${symbols.join(', ')}`)
          }

          if (oldest && newest) {
            this.log(`  ${bold('Data range:')} ${oldest} → ${newest}`)

            // Calculate days of data
            const oldDate = new Date(oldest)
            const newDate = new Date(newest)
            const days = Math.round((newDate.getTime() - oldDate.getTime()) / (1000 * 60 * 60 * 24))
            this.log(`  ${bold('Days collected:')} ${days}`)
          }
        } else {
          this.log(`  ${yellow('!')} No collector database found`)
          this.log(`    ${dim('Start collecting: rift collect start')}`)
        }

        this.log('')
      }
    })
  }
}
