import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {requestTransfer, postToHyperliquid, getExistingSession} from '../lib/walletconnect.js'
import {loadCredentials} from '../lib/credentials.js'
import {green, red, cyan, dim, bold} from '../lib/tui.js'

export default class Transfer extends GatedCommand {
  static override description = 'Transfer USDC between spot and perps on Hyperliquid'

  static override examples = [
    '$ rift transfer 100 --to-perps',
    '$ rift transfer 50 --to-spot',
  ]

  static override args = {
    amount: Args.string({description: 'USDC amount to transfer', required: true}),
  }

  static override flags = {
    'to-perps': Flags.boolean({description: 'Transfer from Spot → Perps', default: false}),
    'to-spot': Flags.boolean({description: 'Transfer from Perps → Spot', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Transfer)
    const amount = args.amount
    const isMainnet = true

    // Determine direction
    let toPerp = true
    if (flags['to-spot']) {
      toPerp = false
    } else if (!flags['to-perps'] && !flags['to-spot']) {
      // Default to to-perps if neither specified
      toPerp = true
    }

    const direction = toPerp ? 'Spot → Perps' : 'Perps → Spot'

    const creds = loadCredentials()
    if (!creds) {
      this.log(`  ${red('✘')} No wallet configured. Run: ${cyan('rift auth setup')}`)
      return
    }

    // Check for existing WalletConnect session
    const session = await getExistingSession()
    if (!session) {
      this.log(`  ${red('✘')} No wallet session. Run: ${cyan('rift auth setup')} to reconnect.`)
      return
    }

    this.log('')
    this.log(`  Transferring $${amount} (${direction})...`)
    this.log(`  ${dim('→ Approve in your wallet (check your phone)')}`)
    this.log('')

    const result = await requestTransfer(amount, toPerp, isMainnet)

    if (!result.success) {
      this.log(`  ${red('✘')} Transfer failed: ${result.error}`)
      return
    }

    // Post to Hyperliquid
    try {
      const response = await postToHyperliquid(result.action!, result.signature!, result.nonce!, isMainnet)
      this.log(`  ${green('✔')} Transfer complete`)
      this.log(`  ${dim(`$${amount} moved ${direction}`)}`)
      this.log(`  ${dim('Run: rift balance to verify')}`)
      this.log('')
    } catch (error: any) {
      this.log(`  ${red('✘')} Failed to submit transfer: ${error.message}`)
    }
  }
}
