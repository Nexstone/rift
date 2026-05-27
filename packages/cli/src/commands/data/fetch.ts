import {Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'
import {runEngine} from '../../lib/python-bridge.js'
import type {EngineMessage} from '../../lib/python-bridge.js'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

export default class DataFetch extends GatedCommand {
  static override description = 'Fetch and cache candle data from Hyperliquid'

  static override examples = [
    '$ rift data fetch --pair BTC-PERP --tf 15m',
    '$ rift data fetch --pair ETH-PERP --tf 1h --start 2025-01-01',
    '$ rift data fetch --all --tf 1h',
    '$ rift data fetch --top 10 --tf 4h',
  ]

  static override flags = {
    pair: Flags.string({description: 'Trading pair (e.g. BTC-PERP)', default: 'BTC-PERP'}),
    tf: Flags.string({description: 'Timeframe / candle interval', default: '1h'}),
    start: Flags.string({description: 'Start date (YYYY-MM-DD)'}),
    all: Flags.boolean({description: 'Fetch top 20 pairs by volume', default: false}),
    top: Flags.integer({description: 'Fetch top N pairs by volume'}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(DataFetch)

    if (flags.all || flags.top) {
      return this.fetchMulti(flags)
    }

    return this.fetchSingle(flags)
  }

  private async fetchSingle(flags: {pair: string; tf?: string; start?: string}): Promise<void> {
    const args: string[] = [flags.pair, '--tf', flags.tf!]
    if (flags.start) args.push('--start', flags.start)

    this.log(`\n  Fetching ${bold(flags.pair)} ${flags.tf} candles...`)

    await runEngine('fetch', args, (msg: EngineMessage) => {
      if (msg.type === 'progress' && msg.msg) {
        process.stdout.write(`\r  ${dim(String(msg.msg))}${''.padEnd(20)}`)
      } else if (msg.type === 'result') {
        process.stdout.write('\r' + ' '.repeat(60) + '\r')
        this.log(`  ${green('✔')} ${bold(String(msg.pair))} — ${msg.candles} candles`)
      } else if (msg.type === 'error') {
        this.error(msg.msg as string)
      }
    })

    this.log('')
  }

  private async fetchMulti(flags: {all: boolean; top?: number; tf?: string; start?: string}): Promise<void> {
    const n = flags.top || 20
    const topArg = `top${n}`

    const args: string[] = [topArg, '--tf', flags.tf!]
    if (flags.start) args.push('--start', flags.start)
    args.push('--top', String(n))

    this.log(`\n  Fetching top ${bold(String(n))} pairs by volume (${flags.tf})...\n`)

    await runEngine('fetch-multi', args, (msg: EngineMessage) => {
      if (msg.type === 'progress' && msg.msg) {
        process.stdout.write(`\r  ${dim(String(msg.msg))}${''.padEnd(30)}`)
      } else if (msg.type === 'result') {
        process.stdout.write('\r' + ' '.repeat(60) + '\r')

        const results = msg.results as Array<{pair: string; candles: number; status: string; error?: string}>
        let succeeded = 0
        let failed = 0

        for (const r of results) {
          if (r.status === 'ok') {
            this.log(`  ${green('✔')} ${bold(r.pair.padEnd(10))} ${dim(`${r.candles} candles`)}`)
            succeeded++
          } else {
            this.log(`  ${red('✘')} ${bold(r.pair.padEnd(10))} ${dim(r.error || 'failed')}`)
            failed++
          }
        }

        this.log('')
        this.log(`  ${dim(`${succeeded} succeeded${failed > 0 ? `, ${failed} failed` : ''}`)}`)
      }
    })

    this.log('')
  }
}
