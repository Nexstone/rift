import {GatedCommand} from '../lib/base-command.js'
import {createInterface} from 'node:readline'

const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`

function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => {
      rl.close()
      resolve(answer.trim())
    })
  })
}

function menu(title: string, options: Array<{key: string; label: string; desc: string}>): void {
  console.log('')
  console.log(`  ${bold(title)}`)
  console.log('')
  for (const opt of options) {
    console.log(`    ${cyan(opt.key)}  ${opt.label}  ${dim(opt.desc)}`)
  }
  console.log('')
}

export default class Interactive extends GatedCommand {
  static override description = 'Launch interactive mode'

  static override examples = [
    '$ rift interactive',
  ]

  async run(): Promise<void> {
    // Main loop — after each action the user returns to the menu.
    // Exit via `0`, `q`, `quit`, or Ctrl-C.
    while (true) {
      console.log('')
      console.log(`  ${bold('⬡ RIFT')} ${dim('v0.1.0')}`)
      console.log(`  ${dim('Research / Iteration / Forecast / Trade')}`)

      menu('What would you like to do?', [
        {key: '1', label: 'Scout the market', desc: '— scan for opportunities now'},
        {key: '2', label: 'Quick trade', desc: '— manual trade with stop loss'},
        {key: '3', label: 'Backtest a strategy', desc: '— test on historical data'},
        {key: '4', label: 'Compare strategies', desc: '— head-to-head comparison'},
        {key: '5', label: 'Fetch market data', desc: '— download candles from Hyperliquid'},
        {key: '6', label: 'Create a new strategy', desc: '— scaffold from template'},
        {key: '7', label: 'Portfolio manager', desc: '— multi-strategy algo trading'},
        {key: '8', label: 'System health check', desc: '— run rift doctor'},
        {key: '9', label: 'Quick start', desc: '— set up everything'},
        {key: '0', label: 'Exit', desc: ''},
      ])

      const action = await ask(`  ${cyan('>')} `)

      switch (action) {
        case '0':
        case 'q':
        case 'quit':
        case 'exit':
          console.log('')
          return
        case '1':
          await this.dispatch('scout')
          break
        case '2':
          await this.dispatch('trade')
          break
        case '3':
          await this.interactiveBacktest()
          break
        case '4':
          await this.interactiveCompare()
          break
        case '5':
          await this.interactiveFetch()
          break
        case '6':
          await this.interactiveNew()
          break
        case '7':
          await this.interactivePortfolio()
          break
        case '8':
          await this.dispatch('doctor')
          break
        case '9':
          await this.dispatch('init')
          break
        default:
          if (action) {
            console.log(dim('  Invalid selection. Pick 0-9.'))
          }
      }
    }
  }

  /**
   * Run a subcommand and pause briefly if it exited fast (likely a
   * precondition failure that printed an error). Prevents the menu redraw
   * from clobbering "Trade requires wallet setup" etc.
   */
  private async dispatch(cmd: string, args: string[] = []): Promise<void> {
    const start = Date.now()
    try {
      await this.config.runCommand(cmd, args)
    } catch (err: any) {
      console.log(`\n  ${dim('Command threw:')} ${err?.message ?? err}\n`)
    }
    const elapsed = Date.now() - start
    if (elapsed < 1500) {
      await ask(`  ${dim('Press Enter to return to menu...')} `)
    }
  }

  private async interactiveBacktest(): Promise<void> {
    const strategy = await ask(`  ${cyan('Strategy name')} ${dim('(e.g. btc_funding_fade, or run rift strategies to see all)')}: `)
    if (!strategy) {
      console.log(dim('  Cancelled — no strategy name given.'))
      return
    }

    const pair = await ask(`  ${cyan('Ticker')} ${dim('(BTC)')}: `) || 'BTC'
    const tf = await ask(`  ${cyan('Timeframe')} ${dim('(1h)')}: `) || '1h'

    console.log('')
    await this.config.runCommand('backtest', [strategy, '--pair', pair, '--tf', tf])
  }

  private async interactiveCompare(): Promise<void> {
    const input = await ask(`  ${cyan('Strategies')} ${dim('(comma-separated, e.g. btc_funding_fade,my_strategy)')}: `)
    if (!input) {
      console.log(dim('  Cancelled — no strategies given.'))
      return
    }

    const pair = await ask(`  ${cyan('Ticker')} ${dim('(BTC)')}: `) || 'BTC'
    const tf = await ask(`  ${cyan('Timeframe')} ${dim('(1h)')}: `) || '1h'

    console.log('')
    await this.config.runCommand('compare', [input, '--pair', pair, '--tf', tf])
  }

  private async interactiveFetch(): Promise<void> {
    const pair = await ask(`  ${cyan('Ticker')} ${dim('(BTC)')}: `) || 'BTC'
    const tf = await ask(`  ${cyan('Timeframe')} ${dim('(1h)')}: `) || '1h'

    console.log('')
    await this.config.runCommand('data:fetch', ['--pair', pair, '--tf', tf])
  }

  private async interactivePortfolio(): Promise<void> {
    menu('Portfolio Manager', [
      {key: '1', label: 'Create portfolio', desc: '— build a portfolio config'},
      {key: '2', label: 'Start portfolio', desc: '— launch supervisor + strategies'},
      {key: '3', label: 'Portfolio status', desc: '— view running portfolio'},
      {key: '4', label: 'Stop portfolio', desc: '— stop all strategies'},
      {key: '5', label: 'View alerts', desc: '— recent trading alerts'},
      {key: '0', label: 'Back', desc: ''},
    ])
    const choice = await ask(`  ${cyan('>')} `)
    switch (choice) {
      case '0':
      case 'q':
      case '':
        return
      case '1': await this.config.runCommand('portfolio:create'); break
      case '2': await this.config.runCommand('portfolio:start'); break
      case '3': await this.config.runCommand('portfolio:status'); break
      case '4': await this.config.runCommand('portfolio:stop'); break
      case '5': await this.config.runCommand('portfolio:alerts'); break
      default:
        console.log(dim('  Invalid selection. Pick 0-5.'))
    }
  }

  private async interactiveNew(): Promise<void> {
    const name = await ask(`  ${cyan('Strategy name')}: `)
    if (!name) {
      console.log(dim('  Cancelled — no strategy name given.'))
      return
    }

    console.log('')
    await this.config.runCommand('new', [name])
  }
}
