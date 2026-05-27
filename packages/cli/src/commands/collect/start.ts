import {Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'
import {runEngine} from '../../lib/python-bridge.js'
import type {EngineMessage} from '../../lib/python-bridge.js'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`

export default class CollectStart extends GatedCommand {
  static override description = 'Start the persistent data collector — builds historical data 24/7'

  static override examples = [
    '$ rift collect start',
    '$ rift collect start --symbols BTC,ETH,SOL,HYPE,DOGE --tf 1h,15m',
    '$ rift collect start --ob-interval 30',
  ]

  static override flags = {
    symbols: Flags.string({description: 'Comma-separated symbols to collect', default: 'BTC,ETH,SOL,HYPE'}),
    tf: Flags.string({description: 'Comma-separated timeframes for candles', default: '1h'}),
    'ob-interval': Flags.integer({description: 'Seconds between order book snapshots', default: 60}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(CollectStart)

    const symbols = flags.symbols!.split(',').map(s => s.trim())
    const timeframes = flags.tf!.split(',').map(t => t.trim())

    this.log('')
    this.log(`  ${bold('RIFT Data Collector')}`)
    this.log(`  ${dim('─'.repeat(55))}`)
    this.log(`  Symbols:    ${symbols.map(s => bold(s)).join(', ')}`)
    this.log(`  Timeframes: ${timeframes.join(', ')}`)
    this.log(`  Order book: every ${flags['ob-interval']}s`)
    this.log(`  ${dim('─'.repeat(55))}`)
    this.log('')
    this.log(`  ${dim('Collecting:')}`)
    this.log(`    ${green('•')} Candle data (OHLCV) for ${symbols.length} pairs`)
    this.log(`    ${green('•')} Funding rates (hourly)`)
    this.log(`    ${green('•')} L2 order book snapshots (${symbols.length} × ${Math.round(86400 / flags['ob-interval']!)}/day)`)
    this.log(`    ${green('•')} Market data (mid prices, funding)`)
    this.log('')
    this.log(`  ${dim('Data stored at: ~/.rift/data/ and ~/.rift/collector.db')}`)
    this.log(`  ${dim('The longer this runs, the more valuable your data becomes.')}`)
    this.log(`  ${dim('Press Ctrl+C to stop')}`)
    this.log('')

    const engineArgs: string[] = [
      '--symbols', flags.symbols!,
      '--tf', flags.tf!,
      '--ob-interval', String(flags['ob-interval']),
    ]

    try {
      await runEngine('collect', engineArgs, (msg: EngineMessage) => {
        if (msg.type === 'status') {
          this.log(`  ${green('✔')} ${msg.msg}`)
        } else if (msg.type === 'heartbeat') {
          const stats = msg.stats as Record<string, number>
          const dbSize = msg.db_size_mb as number
          process.stdout.write(`\r  ${dim(`Uptime: ${msg.uptime_minutes}min | OB: ${stats?.orderbook || 0} | Market: ${stats?.market || 0} | Candles: ${stats?.candles || 0} | DB: ${dbSize}MB`)}${''.padEnd(10)}`)
        } else if (msg.type === 'shutdown') {
          process.stdout.write('\r' + ' '.repeat(100) + '\r')
          this.log('')
          this.log(`  ${dim('Collector stopped.')}`)
          const stats = msg.stats as Record<string, number>
          if (stats) {
            this.log(`  ${dim(`Total collected: ${stats.orderbook} orderbook, ${stats.market} market, ${stats.candles} candles, ${stats.funding} funding`)}`)
          }
          this.log('')
        } else if (msg.type === 'error') {
          this.log(`  ${dim(`Error: ${msg.msg}`)}`)
        }
      })
    } catch (error: any) {
      if (!error.message.includes('null') && !error.message.includes('SIGTERM')) {
        this.log(`  Error: ${error.message.split('\n')[0]}`)
      }
    }
  }
}
