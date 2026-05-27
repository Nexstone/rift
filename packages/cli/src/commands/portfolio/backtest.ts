import {Args} from '@oclif/core'
import * as path from 'node:path'
import {GatedCommand} from '../../lib/base-command.js'
import {runEngine} from '../../lib/python-bridge.js'
import type {EngineMessage} from '../../lib/python-bridge.js'

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

export default class PortfolioBacktest extends GatedCommand {
  static override description = 'Backtest a portfolio of multiple strategies simultaneously'

  static override examples = [
    '$ rift portfolio backtest strategies/configs/portfolio_btc.yaml',
  ]

  static override args = {
    config: Args.string({description: 'Path to portfolio.yaml config file', required: true}),
  }

  async run(): Promise<void> {
    const {args} = await this.parse(PortfolioBacktest)

    this.log('')
    this.log(`  ${bold('Portfolio Backtest')}`)
    this.log(`  ${dim(`Config: ${args.config}`)}`)
    this.log('')

    const configPath = path.resolve(args.config)
    await runEngine('portfolio-backtest', [configPath], (msg: EngineMessage) => {
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
    const strategies = msg.strategies as Array<Record<string, any>>
    const corrMatrix = msg.correlation_matrix as {strategies: string[]; matrix: number[][]} | null

    // Per-strategy results table
    this.log(dim('  ── Per-Strategy Results ──'))
    this.log('')

    const hdr = `  ${dim('│')} ${'Strategy'.padEnd(20)} ${'Alloc'.padEnd(8)} ${'Return'.padEnd(12)} ${'Sharpe'.padEnd(10)} ${'PF'.padEnd(8)} ${'MaxDD'.padEnd(10)} ${'Trades'.padEnd(8)} ${'Win%'.padEnd(8)} ${dim('│')}`
    const hrLen = hdr.replace(/\x1b\[[0-9;]*m/g, '').length
    const hr = `  ${dim('─'.repeat(hrLen - 2))}`

    this.log(hr)
    this.log(hdr)
    this.log(hr)

    for (const s of strategies) {
      const retStr = `${s.return_pct}%`
      const ddStr = `${s.max_drawdown_pct}%`

      let row = `  ${dim('│')} ${bold(String(s.name).padEnd(20))} `
      row += `${String(s.allocation).padEnd(8)} `
      row += `${colorNum(s.return_pct, '%')}${' '.repeat(Math.max(1, 12 - retStr.length))} `
      row += `${colorNum(s.sharpe)}${' '.repeat(Math.max(1, 10 - String(s.sharpe).length))} `
      row += `${String(s.profit_factor).padEnd(8)} `
      row += `${colorNum(s.max_drawdown_pct, '%')}${' '.repeat(Math.max(1, 10 - ddStr.length))} `
      row += `${String(s.num_trades).padEnd(8)} `
      row += `${String(s.win_rate).padEnd(8)} `
      row += dim('│')
      this.log(row)
    }

    this.log(hr)
    this.log('')

    // Portfolio summary box
    const w = 55
    const boxHr = '─'.repeat(w - 2)

    this.log(`  ${dim('┌' + boxHr + '┐')}`)
    this.log(`  ${dim('│')} ${bold('PORTFOLIO SUMMARY')}${' '.repeat(w - 20)}${dim('│')}`)
    this.log(`  ${dim('├' + boxHr + '┤')}`)

    this.log(this.row('Initial Equity', `$${Number(msg.initial_equity).toLocaleString()}`, w))
    this.log(this.row('Final Equity', `$${Number(msg.final_equity).toLocaleString()}`, w))
    this.log(this.rowColored('Portfolio Return', msg.total_return_pct as number, '%', w))
    this.log(this.rowColored('Portfolio Sharpe', msg.portfolio_sharpe as number, '', w))
    this.log(this.rowColored('Portfolio Max Drawdown', msg.portfolio_max_drawdown_pct as number, '%', w))
    this.log(this.row('Total Trades', String(msg.total_trades), w))
    this.log(this.row('Strategies', String(strategies.length), w))

    this.log(`  ${dim('└' + boxHr + '┘')}`)
    this.log('')

    // Correlation matrix
    if (corrMatrix && corrMatrix.strategies.length > 1) {
      this.log(dim('  ── Strategy Correlation ──'))
      this.log('')

      const names = corrMatrix.strategies
      const matrix = corrMatrix.matrix

      // Header
      let corrHdr = `  ${''.padEnd(20)}`
      for (const n of names) {
        corrHdr += `${n.slice(0, 12).padEnd(14)}`
      }
      this.log(dim(corrHdr))

      // Rows
      for (let i = 0; i < names.length; i++) {
        let corrRow = `  ${names[i].padEnd(20)}`
        for (let j = 0; j < names.length; j++) {
          const val = matrix[i][j]
          const valStr = val.toFixed(3)
          if (i === j) {
            corrRow += dim(valStr.padEnd(14))
          } else if (val > 0.7) {
            corrRow += red(valStr.padEnd(14))  // high correlation = warning
          } else if (val < 0.3) {
            corrRow += green(valStr.padEnd(14))  // low correlation = good
          } else {
            corrRow += yellow(valStr.padEnd(14))
          }
        }
        this.log(corrRow)
      }

      this.log('')

      // Check for high correlations
      for (let i = 0; i < names.length; i++) {
        for (let j = i + 1; j < names.length; j++) {
          if (matrix[i][j] > 0.7) {
            this.log(`  ${yellow('!')} ${names[i]} and ${names[j]} are highly correlated (${matrix[i][j].toFixed(3)}) — diversification benefit is limited`)
          }
        }
      }

      const allLow = matrix.every((row, i) => row.every((val, j) => i === j || val < 0.5))
      if (allLow) {
        this.log(`  ${green('✔')} Strategy correlations are low — good portfolio diversification`)
      }

      this.log('')
    }

    // Interpretation
    const portfolioReturn = msg.total_return_pct as number
    const portfolioSharpe = msg.portfolio_sharpe as number
    const portfolioDD = msg.portfolio_max_drawdown_pct as number

    this.log(dim('  ── Interpretation ──'))
    this.log('')

    if (portfolioSharpe > 0.5 && portfolioDD > -10) {
      this.log(`  ${green('Strong portfolio.')} Positive Sharpe with controlled drawdown.`)
    } else if (portfolioSharpe > 0 && portfolioDD > -20) {
      this.log(`  ${yellow('Moderate portfolio.')} Positive returns but drawdown needs attention.`)
    } else {
      this.log(`  ${red('Weak portfolio.')} Consider adjusting allocations or strategy selection.`)
    }

    // Compare portfolio to best individual strategy
    const bestStrat = strategies.reduce((best, s) =>
      (s.sharpe > best.sharpe ? s : best), strategies[0])

    if (portfolioSharpe > bestStrat.sharpe) {
      this.log(`  ${green('Portfolio Sharpe (' + portfolioSharpe.toFixed(3) + ') exceeds best individual strategy (' + bestStrat.name + ': ' + bestStrat.sharpe.toFixed(3) + ').')}`)
      this.log(`  ${dim('Diversification is adding value.')}`)
    } else {
      this.log(`  ${dim(`Best individual Sharpe: ${bestStrat.name} (${bestStrat.sharpe.toFixed(3)}) vs portfolio (${portfolioSharpe.toFixed(3)})`)}`)
    }

    this.log('')
  }

  private row(label: string, value: string, width: number): string {
    const labelStr = `  ${label}:`
    const cleanVal = value.replace(/\x1b\[[0-9;]*m/g, '')
    const padding = width - labelStr.length - cleanVal.length - 3
    return `  ${dim('│')}${labelStr}${' '.repeat(Math.max(1, padding))}${value} ${dim('│')}`
  }

  private rowColored(label: string, value: number, suffix: string, width: number): string {
    const coloredStr = colorNum(value, suffix)
    const cleanStr = `${value}${suffix}`
    const labelStr = `  ${label}:`
    const padding = width - labelStr.length - cleanStr.length - 3
    return `  ${dim('│')}${labelStr}${' '.repeat(Math.max(1, padding))}${coloredStr} ${dim('│')}`
  }
}
