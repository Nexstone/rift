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

export default class MonteCarlo extends GatedCommand {
  static override description = 'Run Monte Carlo simulation to test how much of your backtest was luck vs edge'

  static override examples = [
    '$ rift montecarlo trend_follow --pair BTC --tf 4h',
    '$ rift montecarlo trend_follow --pair BTC --tf 4h --runs 50000',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name', required: true}),
  }

  static override flags = {
    pair: Flags.string({description: 'Trading pair', default: 'BTC-PERP'}),
    tf: Flags.string({description: 'Timeframe', default: '1h'}),
    runs: Flags.integer({description: 'Number of simulations', default: 10000}),
    equity: Flags.integer({description: 'Starting equity', default: 10000}),
    leverage: Flags.integer({description: 'Leverage multiplier', default: 1}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(MonteCarlo)

    this.log('')
    this.log(`  ${bold('Monte Carlo Simulation')}`)
    this.log(`  ${dim(`${args.strategy} on ${flags.pair} ${flags.tf} — ${flags.runs!.toLocaleString()} simulations`)}`)
    this.log('')

    const engineArgs: string[] = [
      args.strategy,
      '--pair', flags.pair!,
      '--tf', flags.tf!,
      '--runs', String(flags.runs),
      '--equity', String(flags.equity),
      '--leverage', String(flags.leverage),
    ]

    await runEngine('montecarlo', engineArgs, (msg: EngineMessage) => {
      if (msg.type === 'progress' && msg.msg) {
        process.stdout.write(`\r  ${dim(String(msg.msg))}${''.padEnd(20)}`)
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
    const ret = msg.return_distribution as Record<string, number>
    const dd = msg.drawdown_distribution as Record<string, number>
    const histogram = msg.histogram as Array<{low: number; high: number; count: number; pct: number}>
    const probProfit = msg.prob_profit as number
    const probRuin = msg.prob_ruin as number
    const originalReturn = msg.original_return_pct as number
    const numTrades = msg.num_trades as number
    const numSims = msg.num_simulations as number

    // ASCII histogram
    this.log(dim('  ── Return Distribution ──'))
    this.log('')

    const maxCount = Math.max(...histogram.map(h => h.count))
    const barWidth = 30

    for (const bucket of histogram) {
      const barLen = Math.round((bucket.count / maxCount) * barWidth)
      const bar = '█'.repeat(barLen)
      const label = `${bucket.low.toFixed(0)}%`.padStart(8)
      const countStr = dim(`(${bucket.pct}%)`)

      // Color bar based on whether bucket is positive or negative
      const midpoint = (bucket.low + bucket.high) / 2
      const coloredBar = midpoint >= 0 ? green(bar) : red(bar)

      this.log(`  ${label} ${coloredBar} ${countStr}`)
    }

    this.log('')

    // Summary box
    const w = 55
    const hr = '─'.repeat(w - 2)

    this.log(`  ${dim('┌' + hr + '┐')}`)
    this.log(`  ${dim('│')} ${bold('MONTE CARLO RESULTS')}${' '.repeat(w - 22)}${dim('│')}`)
    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Strategy', String(msg.strategy), w))
    this.log(this.row('Pair / Interval', `${msg.pair} ${msg.interval}`, w))
    this.log(this.row('Trades', String(numTrades), w))
    this.log(this.row('Simulations', numSims.toLocaleString(), w))
    this.log(this.rowColored('Original Return', originalReturn, '%', w))

    this.log(`  ${dim('├' + hr + '┤')}`)
    this.log(`  ${dim('│')} ${dim('RETURN PERCENTILES')}${' '.repeat(w - 22)}${dim('│')}`)
    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.rowColored('5th (worst case)', ret.p5, '%', w))
    this.log(this.rowColored('10th', ret.p10, '%', w))
    this.log(this.rowColored('25th', ret.p25, '%', w))
    this.log(this.rowColored('50th (median)', ret.p50, '%', w))
    this.log(this.rowColored('75th', ret.p75, '%', w))
    this.log(this.rowColored('90th', ret.p90, '%', w))
    this.log(this.rowColored('95th (best case)', ret.p95, '%', w))

    this.log(`  ${dim('├' + hr + '┤')}`)
    this.log(`  ${dim('│')} ${dim('MAX DRAWDOWN PERCENTILES')}${' '.repeat(w - 28)}${dim('│')}`)
    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.rowColored('5th (worst)', dd.p5, '%', w))
    this.log(this.rowColored('25th', dd.p25, '%', w))
    this.log(this.rowColored('50th (median)', dd.p50, '%', w))
    this.log(this.rowColored('75th', dd.p75, '%', w))
    this.log(this.rowColored('95th (best)', dd.p95, '%', w))

    this.log(`  ${dim('├' + hr + '┤')}`)
    this.log(`  ${dim('│')} ${bold('RISK ASSESSMENT')}${' '.repeat(w - 19)}${dim('│')}`)
    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Probability of Profit', `${probProfit}%`, w, probProfit >= 60 ? 'green' : probProfit >= 40 ? 'yellow' : 'red'))
    this.log(this.row('Probability of Ruin (>50% DD)', `${probRuin}%`, w, probRuin <= 10 ? 'green' : probRuin <= 30 ? 'yellow' : 'red'))
    this.log(this.rowColored('Median Sharpe', msg.median_sharpe as number, '', w))

    this.log(`  ${dim('└' + hr + '┘')}`)
    this.log('')

    // Interpretation
    this.log(dim('  ── Interpretation ──'))
    this.log('')

    if (ret.p5 > 0) {
      this.log(`  ${green('Even in the worst 5% of scenarios, this strategy is profitable.')}`)
      this.log(`  ${dim('The edge is robust — not dependent on lucky trade ordering.')}`)
    } else if (ret.p25 > 0) {
      this.log(`  ${yellow('The strategy is profitable in most scenarios, but the bottom 25% lose money.')}`)
      this.log(`  ${dim('Some of the backtest return may be from favorable trade sequencing.')}`)
    } else if (ret.p50 > 0) {
      this.log(`  ${yellow('Median outcome is positive, but there is significant downside risk.')}`)
      this.log(`  ${dim('The strategy has an edge but is sensitive to trade ordering. Consider tighter risk management.')}`)
    } else {
      this.log(`  ${red('The median outcome is negative. The original backtest result was likely lucky.')}`)
      this.log(`  ${dim('This strategy does not have a reliable edge. Do not trade this.')}`)
    }

    if (probRuin > 30) {
      this.log(`  ${red(`Warning: ${probRuin}% chance of >50% drawdown. This is extremely risky.`)}`)
    }

    this.log('')
  }

  private row(label: string, value: string, width: number, color?: string): string {
    const labelStr = `  ${label}:`
    const cleanVal = value.replace(/\x1b\[[0-9;]*m/g, '')
    const padding = width - labelStr.length - cleanVal.length - 3

    let coloredVal = value
    if (color === 'green') coloredVal = green(value)
    else if (color === 'red') coloredVal = red(value)
    else if (color === 'yellow') coloredVal = yellow(value)

    return `  ${dim('│')}${labelStr}${' '.repeat(Math.max(1, padding))}${coloredVal} ${dim('│')}`
  }

  private rowColored(label: string, value: number, suffix: string, width: number): string {
    const coloredStr = colorNum(value, suffix)
    const cleanStr = `${value}${suffix}`
    const labelStr = `  ${label}:`
    const padding = width - labelStr.length - cleanStr.length - 3

    return `  ${dim('│')}${labelStr}${' '.repeat(Math.max(1, padding))}${coloredStr} ${dim('│')}`
  }
}
