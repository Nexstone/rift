import {Flags} from '@oclif/core'
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

export default class Pairs extends GatedCommand {
  static override description = 'Backtest a pairs/spread trade between two assets (e.g. BTC/ETH)'

  // `pairs-backtest` is the engine command name and was the previous
  // wrapper name; alias kept so scripts and docs that referenced it
  // continue to work.
  static override aliases = ['pairs-backtest']

  static override examples = [
    '$ rift pairs --a BTC --b ETH --tf 1h',
    '$ rift pairs --a BTC --b ETH --entry-z 2.5 --lookback 336',
    '$ rift pairs --a BTC --b ETH --json   # raw JSON for pipelines',
  ]

  static override flags = {
    a: Flags.string({description: 'First asset', default: 'BTC'}),
    b: Flags.string({description: 'Second asset', default: 'ETH'}),
    tf: Flags.string({description: 'Timeframe', default: '1h'}),
    equity: Flags.integer({description: 'Starting equity', default: 10000}),
    lookback: Flags.integer({description: 'Rolling z-score window (candles)', default: 168}),
    'entry-z': Flags.string({description: 'Z-score entry threshold', default: '2.0'}),
    'exit-z': Flags.string({description: 'Z-score exit threshold', default: '0.5'}),
    'stop-z': Flags.string({description: 'Z-score stop loss', default: '4.0'}),
    'max-hold': Flags.integer({description: 'Max hold time (candles)', default: 72}),
    json: Flags.boolean({description: 'Emit raw JSON result instead of the rendered panel', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(Pairs)

    if (!flags.json) {
      this.log('')
      this.log(`  ${bold('Pairs Trading Backtest')}`)
      this.log(`  ${dim(`${flags.a}/${flags.b} spread on ${flags.tf} — z-score entry: ${flags['entry-z']}, exit: ${flags['exit-z']}`)}`)
      this.log('')
    }

    const engineArgs: string[] = [
      '--a', flags.a!,
      '--b', flags.b!,
      '--tf', flags.tf!,
      '--equity', String(flags.equity),
      '--lookback', String(flags.lookback),
      '--entry-z', flags['entry-z']!,
      '--exit-z', flags['exit-z']!,
      '--stop-z', flags['stop-z']!,
      '--max-hold', String(flags['max-hold']),
    ]

    await runEngine('pairs-backtest', engineArgs, (msg: EngineMessage) => {
      if (flags.json) {
        // JSON mode: emit the result payload only, silently consume
        // progress/status so the output is pipe-safe.
        if (msg.type === 'result') {
          const {type: _t, ...rest} = msg
          this.log(JSON.stringify(rest, null, 2))
        } else if (msg.type === 'error') {
          this.error(msg.msg as string)
        }
        return
      }
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
    const w = 55
    const hr = '─'.repeat(w - 2)

    this.log(`  ${dim('┌' + hr + '┐')}`)
    this.log(`  ${dim('│')} ${bold('PAIRS TRADING RESULTS')}${' '.repeat(w - 24)}${dim('│')}`)
    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Spread', `${msg.asset_a}/${msg.asset_b}`, w))
    this.log(this.row('Interval', String(msg.interval), w))
    this.log(this.row('Avg Hold', `${msg.avg_hold_candles}h`, w))

    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Initial', `$${Number(msg.initial_equity).toLocaleString()}`, w))
    this.log(this.row('Final', `$${Number(msg.final_equity).toLocaleString()}`, w))
    this.log(this.rowColored('Return', msg.total_return_pct as number, '%', w))

    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Trades', String(msg.num_trades), w))
    this.log(this.row('Win Rate', `${msg.win_rate}%`, w, (msg.win_rate as number) >= 50 ? 'green' : 'red'))
    this.log(this.rowColored('Avg Win', msg.avg_win_pct as number, '%', w))
    this.log(this.rowColored('Avg Loss', msg.avg_loss_pct as number, '%', w))

    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.rowColored('Max Drawdown', msg.max_drawdown_pct as number, '%', w))
    this.log(this.row('Sharpe Ratio', String(msg.sharpe_ratio), w, (msg.sharpe_ratio as number) > 0.5 ? 'green' : (msg.sharpe_ratio as number) > 0 ? 'yellow' : 'red'))
    this.log(this.row('Profit Factor', String(msg.profit_factor), w, (msg.profit_factor as number) > 1.5 ? 'green' : (msg.profit_factor as number) > 1 ? 'yellow' : 'red'))
    this.log(this.rowColored('Funding P&L', msg.total_funding as number, '', w))

    this.log(`  ${dim('└' + hr + '┘')}`)

    // Interpretation
    this.log('')
    const sharpe = msg.sharpe_ratio as number
    const pf = msg.profit_factor as number
    const dd = msg.max_drawdown_pct as number

    if (sharpe > 0.5 && pf > 1.5 && dd > -10) {
      this.log(`  ${green('Strong pairs trade.')} Positive Sharpe with controlled drawdown.`)
      this.log(`  ${dim('The spread shows mean-reverting behavior — the edge is structural.')}`)
    } else if (sharpe > 0 && pf > 1) {
      this.log(`  ${yellow('Moderate pairs trade.')} Positive but consider optimizing parameters.`)
    } else {
      this.log(`  ${red('Weak or negative.')} The spread may not be mean-reverting in this period.`)
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
