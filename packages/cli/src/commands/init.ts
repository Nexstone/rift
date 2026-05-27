import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {createInterface} from 'node:readline'
import {runEngine} from '../lib/python-bridge.js'
import {hasCredentials} from '../lib/credentials.js'
import {hasApprovedFees, approveFees, BUILDER_FEE_DISPLAY} from '../lib/fees.js'
import type {EngineMessage} from '../lib/python-bridge.js'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const yellow = (s: string) => `\x1b[33m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
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

export default class Init extends GatedCommand {
  static override description = 'Set up RIFT — wallet, sample data, and first backtest in under 60 seconds'

  static override examples = [
    '$ rift init',
  ]

  static override flags = {}

  async run(): Promise<void> {
    const {flags} = await this.parse(Init)

    this.log('')
    this.log(`  ${bold('Welcome to RIFT')} ${dim('— Research / Iteration / Forecast / Trade')}`)
    this.log(`  ${dim('─'.repeat(50))}`)
    this.log('')

    // Step 1: Builder fee approval
    this.log(`  ${dim('1/4')} Builder fee agreement...`)
    if (hasApprovedFees()) {
      this.log(`  ${green('✔')} Builder fee already approved`)
    } else {
      this.log('')
      this.log(`  RIFT is free and open-source. To support ongoing`)
      this.log(`  development, a ${bold(BUILDER_FEE_DISPLAY)} builder fee is applied to live`)
      this.log(`  trades executed through RIFT on Hyperliquid.`)
      this.log('')
      this.log(`  ${dim('Backtesting, simulation, and analysis are always free.')}`)
      this.log(`  ${dim('Example: on a $10,000 perp trade, the fee is $3.')}`)
      this.log('')

      const answer = await ask(`  ${cyan('Do you agree?')} ${dim('(yes/no)')}: `)

      if (answer.toLowerCase() === 'yes' || answer.toLowerCase() === 'y') {
        approveFees()
        this.log(`  ${green('✔')} Builder fee approved. Thank you for supporting RIFT.`)
      } else {
        this.log('')
        this.log(`  ${red('RIFT requires builder fee approval to operate.')}`)
        this.log(`  ${dim('Run rift init again when you\'re ready.')}`)
        this.log('')
        return
      }
    }

    this.log('')

    // Step 2: Check/generate wallet
    this.log(`  ${dim('2/4')} Setting up wallet...`)
    if (hasCredentials()) {
      this.log(`  ${green('✔')} Wallet already configured`)
    } else {
      const {generatePrivateKey, privateKeyToAccount} = await import('viem/accounts')
      const {saveCredentials} = await import('../lib/credentials.js')

      const privateKey = generatePrivateKey()
      const account = privateKeyToAccount(privateKey)

      saveCredentials({
        private_key: privateKey.replace(/^0x/, ''),
        address: account.address.toLowerCase(),
        account_address: account.address.toLowerCase(),
        type: 'generated',
        network: 'mainnet',
        registered_at: new Date().toISOString(),
      })

      this.log(`  ${green('✔')} Wallet generated: ${dim(account.address)}`)
      this.log(`  ${yellow('!')} Save your private key: ${privateKey}`)
    }

    this.log('')

    // Step 3: Fetch sample data
    this.log(`  ${dim('3/4')} Fetching BTC 1h data from Hyperliquid...`)
    try {
      await runEngine('fetch', ['BTC', '--tf', '1h'], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          this.log(`  ${green('✔')} Cached ${msg.candles} candles`)
        }
      })
    } catch {
      this.log(`  ${yellow('!')} Could not fetch data (offline?). You can run 'rift data fetch' later.`)
    }

    this.log('')

    // Step 4: Run a quick backtest
    this.log(`  ${dim('4/4')} Running sample backtest...`)
    try {
      await runEngine('backtest', ['trend_follow', '--pair', 'BTC', '--tf', '4h'], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          const ret = msg.total_return_pct as number
          const trades = msg.num_trades as number
          this.log(`  ${green('✔')} Backtest complete: ${ret > 0 ? green(`+${ret}%`) : `${ret}%`} return, ${trades} trades`)
        }
      })
    } catch {
      this.log(`  ${yellow('!')} Backtest skipped. Run manually: rift backtest trend_follow --pair BTC --tf 4h`)
    }

    this.log('')
    this.log(`  ${dim('─'.repeat(50))}`)
    this.log(`  ${green('✔')} ${bold('RIFT is ready.')}`)
    this.log('')
    this.log(`  ${dim('Try these commands:')}`)
    this.log(`    ${cyan('rift strategies list')}             ${dim('— see available strategies')}`)
    this.log(`    ${cyan('rift backtest trend_follow --pair BTC --tf 4h')}  ${dim('— run a backtest')}`)
    this.log(`    ${cyan('rift guide')}                        ${dim('— 9-step research-to-trade journey')}`)
    this.log(`    ${cyan('rift new my-strategy')}              ${dim('— create your own')}`)
    this.log(`    ${cyan('rift doctor')}                       ${dim('— check system health')}`)
    this.log('')
  }
}
