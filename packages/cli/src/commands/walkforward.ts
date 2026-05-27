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

function gradeBadge(ratio: number): string {
  if (ratio >= 0.7) return green(bold('ROBUST'))
  if (ratio >= 0.4) return yellow(bold('MODERATE'))
  if (ratio > 0) return red(bold('WEAK'))
  return red(bold('OVERFIT'))
}

export default class WalkForward extends GatedCommand {
  static override description = 'Run walk-forward analysis to test strategy robustness'

  static override examples = [
    '$ rift walkforward btc_funding_fade --pair BTC --tf 1h --wf 3m/1m',
    '$ rift walkforward btc_funding_fade --pair BTC --tf 1h --wf 6m/2m',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name', required: true}),
  }

  static override flags = {
    pair: Flags.string({description: 'Trading pair', default: 'BTC-PERP'}),
    tf: Flags.string({description: 'Timeframe', default: '1h'}),
    wf: Flags.string({description: 'Walk-forward config: train/test (e.g. 3m/1m)', default: '3m/1m'}),
    equity: Flags.integer({description: 'Starting equity per window', default: 10000}),
    leverage: Flags.integer({description: 'Leverage multiplier', default: 1}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(WalkForward)

    this.log('')
    this.log(`  ${bold('Walk-Forward Analysis')}`)
    this.log(`  ${dim(`${args.strategy} on ${flags.pair} ${flags.tf} — ${flags.wf} windows`)}`)
    this.log('')

    const engineArgs: string[] = [
      args.strategy,
      '--pair', flags.pair!,
      '--tf', flags.tf!,
      '--wf', flags.wf!,
      '--equity', String(flags.equity),
      '--leverage', String(flags.leverage),
    ]

    await runEngine('walk-forward', engineArgs, (msg: EngineMessage) => {
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
    const windows = msg.windows as any[]
    const is = msg.in_sample as any
    const oos = msg.out_of_sample as any
    const degradation = msg.degradation_ratio as number
    const pctProfitable = msg.pct_profitable_windows as number
    const numWindows = msg.num_windows as number

    // Per-window results table
    this.log(dim('  ── Per-Window Results ──'))
    this.log('')

    const hdr = `  ${dim('│')} ${'#'.padEnd(3)} ${'Train Period'.padEnd(24)} ${'Test Period'.padEnd(24)} ${'IS Return'.padEnd(12)} ${'OOS Return'.padEnd(12)} ${'OOS Sharpe'.padEnd(12)} ${dim('│')}`
    const hr = `  ${dim('─'.repeat(hdr.replace(/\x1b\[[0-9;]*m/g, '').length - 2))}`

    this.log(hr)
    this.log(hdr)
    this.log(hr)

    for (const w of windows) {
      const isRet = colorNum(w.in_sample.return_pct, '%')
      const oosRet = colorNum(w.out_of_sample.return_pct, '%')
      const oosSharpe = colorNum(w.out_of_sample.sharpe)

      const isRetClean = `${w.in_sample.return_pct}%`
      const oosRetClean = `${w.out_of_sample.return_pct}%`
      const oosSharpeClean = `${w.out_of_sample.sharpe}`

      const trainPeriod = `${w.train_period.start} → ${w.train_period.end}`
      const testPeriod = `${w.test_period.start} → ${w.test_period.end}`

      this.log(`  ${dim('│')} ${String(w.window).padEnd(3)} ${trainPeriod.padEnd(24)} ${testPeriod.padEnd(24)} ${isRet}${' '.repeat(Math.max(1, 12 - isRetClean.length))} ${oosRet}${' '.repeat(Math.max(1, 12 - oosRetClean.length))} ${oosSharpe}${' '.repeat(Math.max(1, 12 - oosSharpeClean.length))} ${dim('│')}`)
    }

    this.log(hr)
    this.log('')

    // Summary box
    const w = 55
    const boxHr = '─'.repeat(w - 2)

    this.log(`  ${dim('┌' + boxHr + '┐')}`)
    this.log(`  ${dim('│')} ${bold('WALK-FORWARD SUMMARY')}${' '.repeat(w - 23)}${dim('│')}`)
    this.log(`  ${dim('├' + boxHr + '┤')}`)

    this.log(this.row('Strategy', String(msg.strategy), w))
    this.log(this.row('Pair / Interval', `${msg.pair} ${msg.interval}`, w))
    this.log(this.row('Config', String(msg.config), w))
    this.log(this.row('Windows', String(numWindows), w))

    this.log(`  ${dim('├' + boxHr + '┤')}`)
    this.log(`  ${dim('│')} ${dim('IN-SAMPLE (train periods)')}${' '.repeat(w - 29)}${dim('│')}`)
    this.log(`  ${dim('├' + boxHr + '┤')}`)

    this.log(this.rowColored('Avg Return', is.avg_return_pct, '%', w))
    this.log(this.rowColored('Avg Sharpe', is.avg_sharpe, '', w))
    this.log(this.row('Avg Win Rate', `${is.avg_win_rate}%`, w))
    this.log(this.rowColored('Avg Max Drawdown', is.avg_max_drawdown_pct, '%', w))
    this.log(this.row('Total Trades', String(is.total_trades), w))

    this.log(`  ${dim('├' + boxHr + '┤')}`)
    this.log(`  ${dim('│')} ${bold('OUT-OF-SAMPLE (test periods — unseen data)')}${' '.repeat(w - 46)}${dim('│')}`)
    this.log(`  ${dim('├' + boxHr + '┤')}`)

    this.log(this.rowColored('Avg Return', oos.avg_return_pct, '%', w))
    this.log(this.rowColored('Avg Sharpe', oos.avg_sharpe, '', w))
    this.log(this.row('Avg Win Rate', `${oos.avg_win_rate}%`, w))
    this.log(this.rowColored('Avg Max Drawdown', oos.avg_max_drawdown_pct, '%', w))
    this.log(this.row('Total Trades', String(oos.total_trades), w))
    this.log(this.rowColored('Combined OOS Return', oos.combined_return_pct, '%', w))

    this.log(`  ${dim('├' + boxHr + '┤')}`)
    this.log(`  ${dim('│')} ${bold('ROBUSTNESS')}${' '.repeat(w - 14)}${dim('│')}`)
    this.log(`  ${dim('├' + boxHr + '┤')}`)

    // Degradation ratio with badge
    const badge = gradeBadge(degradation)
    const degStr = `${degradation} — ${badge}`
    // Can't easily calc ANSI-free length of badge, so just pad generously
    this.log(`  ${dim('│')}  Degradation Ratio:  ${degStr}${' '.repeat(Math.max(1, 10))}${dim('│')}`)
    this.log(this.row('Profitable Windows', `${pctProfitable}%`, w, pctProfitable >= 60 ? 'green' : pctProfitable >= 40 ? 'yellow' : 'red'))

    this.log(`  ${dim('└' + boxHr + '┘')}`)
    this.log('')

    // Interpretation
    this.log(dim('  ── Interpretation ──'))
    this.log('')
    if (degradation >= 0.7) {
      this.log(`  ${green('Strategy shows strong out-of-sample performance.')}`)
      this.log(`  ${dim('The strategy maintains most of its edge on unseen data — this is a good sign for live trading.')}`)
    } else if (degradation >= 0.4) {
      this.log(`  ${yellow('Strategy shows moderate degradation on unseen data.')}`)
      this.log(`  ${dim('Some of the backtest performance is likely from curve-fitting. Consider simplifying parameters.')}`)
    } else if (degradation > 0) {
      this.log(`  ${red('Strategy shows significant degradation on unseen data.')}`)
      this.log(`  ${dim('Most of the backtest edge disappears out-of-sample. High risk of failure in live trading.')}`)
    } else {
      this.log(`  ${red('Strategy is likely overfit.')}`)
      this.log(`  ${dim('Out-of-sample performance is negative — the strategy loses money on new data. Do not trade this.')}`)
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
