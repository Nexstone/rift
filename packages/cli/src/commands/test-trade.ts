import {GatedCommand} from '../lib/base-command.js'
import {loadCredentials, hasFullSetup, getAccountAddress} from '../lib/credentials.js'
import {runEngine, getEngineProcess} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'
import {createInterface} from 'node:readline'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => { rl.close(); resolve(answer.trim()) })
  })
}

export default class TestTrade extends GatedCommand {
  static override description = 'Place a minimum-size test trade to verify exchange connectivity'

  static override examples = [
    '$ rift test-trade',
  ]

  async run(): Promise<void> {
    if (!hasFullSetup()) {
      this.log('')
      this.log(`  ${red('Not set up.')} Run ${cyan('rift auth setup')} first.`)
      this.log('')
      return
    }

    const creds = loadCredentials()!

    this.log('')
    this.log(`  ${bold('╔═══════════════════════════════════════════╗')}`)
    this.log(`  ${bold('║          RIFT Exchange Test                ║')}`)
    this.log(`  ${bold('╚═══════════════════════════════════════════╝')}`)
    this.log('')
    this.log(`  This will:`)
    this.log(`    ${cyan('1.')} Connect to Hyperliquid`)
    this.log(`    ${cyan('2.')} Place a minimum-size BTC long ($10)`)
    this.log(`    ${cyan('3.')} Verify the stop loss is placed`)
    this.log(`    ${cyan('4.')} Wait 10 seconds`)
    this.log(`    ${cyan('5.')} Close the position`)
    this.log(`    ${cyan('6.')} Report results`)
    this.log('')
    const mainAddr = getAccountAddress(creds)
    this.log(`  ${dim('Wallet:')}  ${mainAddr}`)
    this.log(`  ${dim('Cost:')}    ~$0.07 in fees (2x $0.035 per side)`)
    this.log('')

    const confirm = await ask(`  ${cyan('Run test?')} ${dim('(yes/no)')}: `)
    if (confirm.toLowerCase() !== 'yes' && confirm.toLowerCase() !== 'y') {
      this.log(dim('\n  Cancelled.\n'))
      return
    }

    this.log('')

    const engineArgs: string[] = [
      '--private-key', creds.private_key,
      '--account', mainAddr,
    ]

    try {
      await runEngine('test-trade', [
        '--account', mainAddr,
      ], (msg: EngineMessage) => {
        if (msg.type === 'status') {
          const icon = String(msg.msg).includes('✔') ? '' : '  '
          this.log(`${icon}${msg.msg}`)
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        } else if (msg.type === 'result') {
          this.log('')
          this.log(`  ${bold('═'.repeat(45))}`)
          this.log('')
          if (msg.success) {
            this.log(`  ${green('✔ TEST PASSED')} — Exchange connectivity verified`)
            this.log('')
            this.log(`  ${dim('Entry price:')}  $${msg.entry_price}`)
            this.log(`  ${dim('Exit price:')}   $${msg.exit_price}`)
            this.log(`  ${dim('P&L:')}          $${msg.pnl}`)
            this.log(`  ${dim('Stop loss:')}    ${msg.stop_placed ? green('✔ placed') : red('✘ failed')}`)
            this.log(`  ${dim('Close:')}        ${msg.close_success ? green('✔ clean') : red('✘ failed')}`)
          } else {
            this.log(`  ${red('✘ TEST FAILED')} — ${msg.error || 'Unknown error'}`)
          }
          this.log('')
          this.log(`  ${bold('═'.repeat(45))}`)
          this.log('')
        }
      }, {HYPERLIQUID_PRIVATE_KEY: creds.private_key})
    } catch (error: any) {
      this.log(`  ${red('✘')} Test failed: ${error?.message}`)
    } finally {
      // Clean up private key from environment
      delete process.env.HYPERLIQUID_PRIVATE_KEY
    }
  }
}
