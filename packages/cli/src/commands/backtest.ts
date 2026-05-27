import {Flags, Args} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {runEngine} from '../lib/python-bridge.js'
import {analyzeBacktest} from '../lib/analyzer.js'
import type {EngineMessage} from '../lib/python-bridge.js'

// ANSI color helpers
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

export default class Backtest extends GatedCommand {
  static override description = 'Run a backtest on cached candle data'

  static override examples = [
    '$ rift backtest btc_funding_fade --pair BTC --tf 1h',
    '$ rift backtest my_strategy --pair BTC --tf 1h --equity 50000',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name', required: true}),
  }

  static override flags = {
    pair: Flags.string({description: 'Trading pair', default: 'BTC-PERP'}),
    tf: Flags.string({description: 'Timeframe (auto-detected from strategy if omitted)'}),
    equity: Flags.integer({description: 'Starting equity in USDC', default: 10000}),
    leverage: Flags.integer({description: 'Leverage multiplier', default: 1}),
    export: Flags.string({description: 'Export results to file (csv or json)', options: ['csv', 'json']}),
    analyze: Flags.boolean({description: 'AI-powered analysis of backtest results', default: false}),
    'all-pairs': Flags.boolean({description: 'Run across top pairs and rank results', default: false}),
    top: Flags.integer({description: 'Number of top pairs for --all-pairs', default: 10}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Backtest)

    const tfDisplay = flags.tf || 'auto'
    const mode = flags['all-pairs'] ? `top ${flags.top} pairs` : flags.pair

    this.log('')
    this.log(dim(`  Backtesting ${bold(args.strategy)} on ${mode} ${tfDisplay}...`))
    this.log('')

    const engineArgs: string[] = [
      args.strategy,
      '--pair', flags.pair!,
      '--equity', String(flags.equity),
      '--leverage', String(flags.leverage),
    ]
    if (flags.tf) engineArgs.push('--tf', flags.tf)
    if (flags['all-pairs']) {
      engineArgs.push('--all-pairs')
      engineArgs.push('--top', String(flags.top))
    }

    let resultMsg: EngineMessage | null = null

    await runEngine('backtest', engineArgs, (msg: EngineMessage) => {
      if (msg.type === 'progress') {
        if (msg.equity != null) {
          const bar = this.progressBar(msg.pct as number)
          process.stdout.write(`\r  ${bar} ${String(msg.pct).padStart(3)}%  equity: $${msg.equity}`)
        } else {
          process.stdout.write(`\r  ${dim(String(msg.msg || ''))}${''.padEnd(20)}`)
        }
      } else if (msg.type === 'result') {
        process.stdout.write('\r' + ' '.repeat(80) + '\r')
        if (msg.command === 'backtest-all-pairs') {
          this.renderAllPairs(msg)
        } else {
          resultMsg = msg
          this.renderResult(msg)
        }
      } else if (msg.type === 'error') {
        this.error(msg.msg as string)
      }
    })

    // Handle export
    if (flags.export && resultMsg) {
      this.exportResults(resultMsg, flags.export, args.strategy, flags.pair!, flags.tf!)
    }

    // Handle AI analysis
    if (flags.analyze && resultMsg) {
      await this.runAnalysis(resultMsg)
    }
  }

  private async runAnalysis(msg: EngineMessage): Promise<void> {
    this.log('')
    this.log(dim('  ── AI Analysis ──'))
    this.log('')

    try {
      this.log(dim('  Thinking...'))
      const analysis = await analyzeBacktest(msg as Record<string, unknown>)

      // Move cursor up to overwrite "Thinking..."
      process.stdout.write('\x1b[1A\x1b[2K')

      // Print analysis with nice formatting
      const lines = analysis.split('\n')
      for (const line of lines) {
        this.log(`  ${line}`)
      }

      this.log('')
    } catch (error: any) {
      process.stdout.write('\x1b[1A\x1b[2K')
      const errMsg = error.message || String(error)
      if (errMsg.includes('api_key') || errMsg.includes('API key') || errMsg.includes('authentication')) {
        this.log(`  ${red('✘')} No API key configured.`)
        this.log('')
        this.log(`  ${dim('Set your Anthropic API key:')}`)
        this.log(`    ${cyan('rift config set ai.api_key sk-ant-...')}`)
        this.log(`  ${dim('Or set environment variable:')}`)
        this.log(`    ${cyan('export RIFT_AI_API_KEY=sk-ant-...')}`)
      } else {
        this.log(`  ${red('✘')} Analysis failed: ${errMsg.split('\n')[0]}`)
      }

      this.log('')
    }
  }

  private exportResults(msg: EngineMessage, format: string, strategy: string, pair: string, tf: string): void {
    const exportData = msg.export as {trades: any[]; equity_curve: number[]; [k: string]: unknown} | undefined
    if (!exportData) {
      this.log(dim('  No export data available.'))
      return
    }

    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
    const baseName = `rift-${strategy}-${pair}-${tf}-${timestamp}`

    if (format === 'json') {
      const filePath = `${baseName}.json`
      fs.writeFileSync(filePath, JSON.stringify(exportData, null, 2))
      this.log(`  ${green('✔')} Exported to ${bold(filePath)}`)
    } else if (format === 'csv') {
      // Metrics CSV
      const metricsPath = `${baseName}-metrics.csv`
      const {trades, equity_curve, ...metrics} = exportData
      const metricsHeaders = Object.keys(metrics).join(',')
      const metricsValues = Object.values(metrics).join(',')
      fs.writeFileSync(metricsPath, `${metricsHeaders}\n${metricsValues}\n`)

      // Trades CSV
      const tradesPath = `${baseName}-trades.csv`
      if (trades && trades.length > 0) {
        const tradeHeaders = Object.keys(trades[0]).join(',')
        const tradeRows = trades.map((t: any) => Object.values(t).join(',')).join('\n')
        fs.writeFileSync(tradesPath, `${tradeHeaders}\n${tradeRows}\n`)
      } else {
        fs.writeFileSync(tradesPath, 'No trades\n')
      }

      this.log(`  ${green('✔')} Exported to:`)
      this.log(`    ${bold(metricsPath)}`)
      this.log(`    ${bold(tradesPath)}`)
    }

    this.log('')
  }

  private renderResult(msg: EngineMessage): void {
    const totalReturn = msg.total_return_pct as number
    const maxDD = msg.max_drawdown_pct as number
    const sharpe = msg.sharpe_ratio as number
    const pf = msg.profit_factor as number
    const winRate = msg.win_rate as number

    // Equity curve chart
    const chart = msg.chart as string[] | undefined
    if (chart && chart.length > 0) {
      this.log(dim('  ── Equity Curve ──'))
      this.log('')
      for (const line of chart) {
        // Color the bars based on position relative to initial equity
        this.log(`  ${cyan(line)}`)
      }
      this.log('')
    }

    // Results box
    const w = 50
    const hr = '─'.repeat(w - 2)

    this.log(`  ${dim('┌' + hr + '┐')}`)
    this.log(`  ${dim('│')} ${bold('BACKTEST RESULTS')}${' '.repeat(w - 19)}${dim('│')}`)
    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Strategy', String(msg.strategy), w))
    this.log(this.row('Pair', String(msg.pair), w))
    this.log(this.row('Interval', String(msg.interval), w))

    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Initial', `$${Number(msg.initial_equity).toLocaleString()}`, w))
    this.log(this.row('Final', `$${Number(msg.final_equity).toLocaleString()}`, w))
    this.log(this.rowColored('Return', totalReturn, '%', w))

    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.row('Trades', String(msg.num_trades), w))
    this.log(this.row('Win Rate', `${winRate}%`, w, winRate >= 50 ? 'green' : winRate >= 40 ? 'yellow' : 'red'))
    this.log(this.row('Wins / Losses', `${msg.wins} / ${msg.losses}`, w))
    this.log(this.rowColored('Avg Win', msg.avg_win_pct as number, '%', w))
    this.log(this.rowColored('Avg Loss', msg.avg_loss_pct as number, '%', w))
    this.log(this.rowColored('Best Trade', msg.best_trade_pct as number, '%', w))
    this.log(this.rowColored('Worst Trade', msg.worst_trade_pct as number, '%', w))

    this.log(`  ${dim('├' + hr + '┤')}`)

    this.log(this.rowColored('Max Drawdown', maxDD, '%', w))
    this.log(this.row('Sharpe Ratio', String(sharpe), w, sharpe > 1 ? 'green' : sharpe > 0 ? 'yellow' : 'red'))
    this.log(this.row('Profit Factor', String(pf), w, pf > 1.5 ? 'green' : pf > 1 ? 'yellow' : 'red'))
    this.log(this.row('Max Consec Wins', String(msg.max_consec_wins), w))
    this.log(this.row('Max Consec Losses', String(msg.max_consec_losses), w))

    // Funding info
    const totalFunding = msg.total_funding as number
    if (totalFunding !== 0 && totalFunding != null) {
      this.log(`  ${dim('├' + hr + '┤')}`)
      this.log(this.rowColored('Funding P&L', totalFunding, '', w))
    }

    this.log(`  ${dim('└' + hr + '┘')}`)
    this.log('')
  }

  private row(label: string, value: string, width: number, color?: string): string {
    const labelStr = `  ${label}:`
    // Strip ANSI for length calc
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

  private renderAllPairs(msg: EngineMessage): void {
    const results = msg.results as Array<Record<string, any>>
    const strategy = msg.strategy as string
    const interval = msg.interval as string

    this.log(bold(`  ${strategy} across ${results.length} pairs (${interval})`))
    this.log('')

    if (!results || results.length === 0) {
      this.log(dim('  No results.'))
      return
    }

    // Table header
    const hdr = `  ${dim('│')} ${'#'.padEnd(3)} ${'Pair'.padEnd(10)} ${'Return'.padEnd(10)} ${'Sharpe'.padEnd(10)} ${'PF'.padEnd(8)} ${'MaxDD'.padEnd(10)} ${'Win%'.padEnd(8)} ${'Trades'.padEnd(8)} ${'Funding'.padEnd(10)} ${dim('│')}`
    const hrLen = hdr.replace(/\x1b\[[0-9;]*m/g, '').length
    const hr = `  ${dim('─'.repeat(hrLen - 2))}`

    this.log(hr)
    this.log(hdr)
    this.log(hr)

    for (let i = 0; i < results.length; i++) {
      const r = results[i]
      const retStr = `${r.return_pct}%`
      const ddStr = `${r.max_drawdown_pct}%`

      let row = `  ${dim('│')} ${bold(String(i + 1).padEnd(3))} ${r.pair.padEnd(10)} `
      row += `${colorNum(r.return_pct, '%')}${' '.repeat(Math.max(1, 10 - retStr.length))} `
      row += `${colorNum(r.sharpe)}${' '.repeat(Math.max(1, 10 - String(r.sharpe).length))} `
      row += `${String(r.profit_factor).padEnd(8)} `
      row += `${colorNum(r.max_drawdown_pct, '%')}${' '.repeat(Math.max(1, 10 - ddStr.length))} `
      row += `${String(r.win_rate).padEnd(8)} `
      row += `${String(r.num_trades).padEnd(8)} `
      row += `${colorNum(r.total_funding)}${' '.repeat(Math.max(1, 10 - String(r.total_funding).length))} `
      row += dim('│')
      this.log(row)
    }

    this.log(hr)
    this.log('')

    // Best pair
    if (results.length > 1) {
      const best = results[0]
      this.log(`  ${green('★')} Best by Sharpe: ${bold(best.pair)} (${colorNum(best.return_pct, '% return')}, Sharpe ${colorNum(best.sharpe)})`)
      this.log('')
    }
  }

  private progressBar(pct: number): string {
    const width = 25
    const filled = Math.round(width * pct / 100)
    const empty = width - filled
    return dim('[') + green('█'.repeat(filled)) + dim('░'.repeat(empty)) + dim(']')
  }
}
