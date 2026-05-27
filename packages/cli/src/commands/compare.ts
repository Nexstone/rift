import {Flags, Args} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const yellow = (s: string) => `\x1b[33m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`

function colorNum(val: number, suffix = ''): string {
  const str = `${val}${suffix}`
  if (val > 0) return green(str)
  if (val < 0) return red(str)
  return yellow(str)
}

function bestOf(results: any[], key: string, higher = true): number {
  const values = results.map(r => r[key] as number)
  return higher ? Math.max(...values) : Math.min(...values)
}

export default class Compare extends GatedCommand {
  static override description = 'Compare multiple strategies head-to-head'

  static override examples = [
    '$ rift compare btc_funding_fade,my_strategy --pair BTC',
  ]

  static override args = {
    strategies: Args.string({description: 'Comma-separated strategy names', required: true}),
  }

  static override flags = {
    pair: Flags.string({description: 'Trading pair', default: 'BTC-PERP'}),
    tf: Flags.string({description: 'Timeframe', default: '15m'}),
    equity: Flags.integer({description: 'Starting equity in USDC', default: 10000}),
    leverage: Flags.integer({description: 'Leverage multiplier', default: 1}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Compare)

    this.log('')
    this.log(dim(`  Comparing strategies on ${flags.pair} ${flags.tf}...`))
    this.log('')

    const engineArgs: string[] = [
      args.strategies,
      '--pair', flags.pair!,
      '--tf', flags.tf!,
      '--equity', String(flags.equity),
      '--leverage', String(flags.leverage),
    ]

    await runEngine('compare', engineArgs, (msg: EngineMessage) => {
      if (msg.type === 'progress' && msg.msg) {
        process.stdout.write(`\r  ${dim(String(msg.msg))}${''.padEnd(40)}`)
      } else if (msg.type === 'result') {
        process.stdout.write('\r' + ' '.repeat(80) + '\r')
        this.renderComparison(msg.results as any[])
      } else if (msg.type === 'error') {
        this.error(msg.msg as string)
      }
    })
  }

  private renderComparison(results: any[]): void {
    if (!results || results.length === 0) {
      this.log('  No results.')
      return
    }

    // Column widths
    const labelW = 18
    const colW = 18

    // Header
    const hr = '─'.repeat(labelW + colW * results.length + 3)
    this.log(`  ${dim(hr)}`)

    let header = `  ${dim('│')} ${'Metric'.padEnd(labelW)}`
    for (const r of results) {
      header += `${dim('│')} ${bold(cyan(String(r.strategy).padEnd(colW - 2)))} `
    }
    header += dim('│')
    this.log(header)
    this.log(`  ${dim(hr)}`)

    // Metrics rows
    const metrics: Array<{label: string; key: string; suffix: string; higher: boolean; format?: (v: any) => string}> = [
      {label: 'Return', key: 'total_return_pct', suffix: '%', higher: true},
      {label: 'Final Equity', key: 'final_equity', suffix: '', higher: true, format: v => `$${Number(v).toLocaleString()}`},
      {label: 'Trades', key: 'num_trades', suffix: '', higher: false},
      {label: 'Win Rate', key: 'win_rate', suffix: '%', higher: true},
      {label: 'Avg Win', key: 'avg_win_pct', suffix: '%', higher: true},
      {label: 'Avg Loss', key: 'avg_loss_pct', suffix: '%', higher: false},
      {label: 'Max Drawdown', key: 'max_drawdown_pct', suffix: '%', higher: false},
      {label: 'Sharpe Ratio', key: 'sharpe_ratio', suffix: '', higher: true},
      {label: 'Profit Factor', key: 'profit_factor', suffix: '', higher: true},
    ]

    for (const m of metrics) {
      const best = bestOf(results, m.key, m.higher)

      let row = `  ${dim('│')} ${m.label.padEnd(labelW)}`
      for (const r of results) {
        const val = r[m.key] as number
        let display: string

        if (m.format) {
          display = m.format(val)
        } else {
          display = `${val}${m.suffix}`
        }

        // Highlight best
        const isBest = val === best && results.length > 1
        const cleanLen = display.replace(/\x1b\[[0-9;]*m/g, '').length

        if (m.key === 'total_return_pct' || m.key === 'max_drawdown_pct' || m.key === 'avg_win_pct' || m.key === 'avg_loss_pct' || m.key === 'sharpe_ratio') {
          display = colorNum(val, m.suffix)
        }

        if (isBest) {
          display = bold(display) + ' ★'
          row += `${dim('│')} ${display}${' '.repeat(Math.max(0, colW - cleanLen - 4))} `
        } else {
          row += `${dim('│')} ${display}${' '.repeat(Math.max(0, colW - cleanLen - 2))} `
        }
      }
      row += dim('│')
      this.log(row)
    }

    this.log(`  ${dim(hr)}`)

    // Winner
    const bestReturn = bestOf(results, 'total_return_pct', true)
    const winner = results.find(r => r.total_return_pct === bestReturn)
    if (winner && results.length > 1) {
      this.log('')
      this.log(`  ${green('★')} Winner by return: ${bold(cyan(winner.strategy))} ${green(`(${winner.total_return_pct}%)`)}`)
    }

    this.log('')
  }
}
