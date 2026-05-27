import {Flags, Args} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const yellow = (s: string) => `\x1b[33m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

function colorNum(val: number, suffix = ''): string {
  const str = `${val}${suffix}`
  if (val > 0) return green(str)
  if (val < 0) return red(str)
  return yellow(str)
}

export default class Sweep extends GatedCommand {
  static override description = 'Run a parameter sweep to find optimal strategy settings'

  static override examples = [
    '$ rift sweep btc_funding_fade --pair BTC --tf 1h',
    '$ rift sweep btc_funding_fade --config strategies/btc_funding_fade/sweep.yaml',
    '$ rift sweep btc_funding_fade --pair BTC --rank sharpe --top 5',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name', required: true}),
  }

  static override flags = {
    pair: Flags.string({description: 'Trading pair', default: 'BTC-PERP'}),
    tf: Flags.string({description: 'Timeframe', default: '1h'}),
    config: Flags.string({description: 'Path to sweep.yaml config file'}),
    equity: Flags.integer({description: 'Starting equity', default: 10000}),
    leverage: Flags.integer({description: 'Leverage multiplier', default: 1}),
    top: Flags.integer({description: 'Number of top results to show', default: 10}),
    rank: Flags.string({description: 'Rank by: sharpe, return, or profit_factor', default: 'sharpe', options: ['sharpe', 'return', 'profit_factor']}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Sweep)

    this.log('')
    this.log(`  ${bold('Parameter Sweep')}`)
    this.log(`  ${dim(`${args.strategy} on ${flags.pair} ${flags.tf} — ranked by ${flags.rank}`)}`)
    this.log('')

    const engineArgs: string[] = [
      args.strategy,
      '--pair', flags.pair!,
      '--tf', flags.tf!,
      '--equity', String(flags.equity),
      '--leverage', String(flags.leverage),
      '--top', String(flags.top),
      '--rank', flags.rank!,
    ]
    if (flags.config) engineArgs.push('--config', flags.config)

    await runEngine('sweep', engineArgs, (msg: EngineMessage) => {
      if (msg.type === 'progress' && msg.msg) {
        // Compact progress display — just combo count + ETA
        const msgStr = String(msg.msg)
        const match = msgStr.match(/Combo (\d+)\/(\d+)(.*?ETA \S+)?/)
        const display = match
          ? `Combo ${match[1]}/${match[2]}${match[3] ? ' — ' + match[3].trim() : ''}`
          : msgStr
        process.stdout.write(`\x1b[2K\r  ${dim(display)}`)
      } else if (msg.type === 'result') {
        process.stdout.write('\r' + ' '.repeat(80) + '\r')
        this.renderResult(msg)
      } else if (msg.type === 'error') {
        process.stdout.write('\r' + ' '.repeat(80) + '\r')
        this.error(msg.msg as string)
      }
    })
  }

  private renderResult(msg: EngineMessage): void {
    const total = msg.total_combos as number
    const completed = msg.completed as number
    const rankBy = msg.rank_by as string
    const topEntries = msg.top as Array<{params: Record<string, any>; metrics: Record<string, any>}>

    this.log(`  ${dim(`Tested ${completed}/${total} parameter combinations`)}`)
    this.log('')

    if (!topEntries || topEntries.length === 0) {
      this.log(`  ${yellow('No results.')}`)
      return
    }

    // Get all param keys for the header
    const paramKeys = Object.keys(topEntries[0].params)

    // Build table header
    const rankLabel = rankBy === 'return' ? 'Return' : rankBy === 'profit_factor' ? 'PF' : 'Sharpe'

    this.log(dim('  ── Top Results ──'))
    this.log('')

    // Header
    let header = `  ${dim('│')} ${'#'.padEnd(4)}`
    for (const key of paramKeys) {
      header += `${key.padEnd(14)}`
    }
    header += `${'Return'.padEnd(12)}${'Sharpe'.padEnd(12)}${'Win%'.padEnd(10)}${'Trades'.padEnd(8)}${'MaxDD'.padEnd(12)} ${dim('│')}`

    const hrLen = header.replace(/\x1b\[[0-9;]*m/g, '').length
    const hr = `  ${dim('─'.repeat(hrLen - 2))}`

    this.log(hr)
    this.log(header)
    this.log(hr)

    for (let i = 0; i < topEntries.length; i++) {
      const e = topEntries[i]
      const m = e.metrics

      let row = `  ${dim('│')} ${bold(String(i + 1).padEnd(4))}`
      for (const key of paramKeys) {
        row += `${String(e.params[key]).padEnd(14)}`
      }

      const retStr = `${m.total_return_pct}%`
      const sharpeStr = `${m.sharpe_ratio}`
      const winStr = `${m.win_rate}%`
      const tradesStr = `${m.num_trades}`
      const ddStr = `${m.max_drawdown_pct}%`

      row += `${colorNum(m.total_return_pct as number, '%')}${' '.repeat(Math.max(1, 12 - retStr.length))}`
      row += `${colorNum(m.sharpe_ratio as number)}${' '.repeat(Math.max(1, 12 - sharpeStr.length))}`
      row += `${winStr.padEnd(10)}`
      row += `${tradesStr.padEnd(8)}`
      row += `${colorNum(m.max_drawdown_pct as number, '%')}${' '.repeat(Math.max(1, 12 - ddStr.length))}`
      row += dim('│')

      this.log(row)
    }

    this.log(hr)

    // Show best config
    const best = topEntries[0]
    this.log('')
    this.log(`  ${green('★')} Best by ${rankLabel}:`)
    for (const [key, val] of Object.entries(best.params)) {
      this.log(`    ${key}: ${bold(String(val))}`)
    }
    this.log(`    → ${colorNum(best.metrics.total_return_pct as number, '% return')}, Sharpe ${colorNum(best.metrics.sharpe_ratio as number)}`)
    this.log('')
  }
}
