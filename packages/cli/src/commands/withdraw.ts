import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {requestWithdrawal, postToHyperliquid, getExistingSession} from '../lib/walletconnect.js'
import {loadCredentials, getAccountAddress} from '../lib/credentials.js'
import {green, red, cyan, dim, bold} from '../lib/tui.js'

export default class Withdraw extends GatedCommand {
  static override description = 'Withdraw USDC from Hyperliquid to Arbitrum'

  static override examples = [
    '$ rift withdraw 100',
    '$ rift withdraw 50 --destination 0x1234...',
  ]

  static override args = {
    amount: Args.string({description: 'USDC amount to withdraw', required: true}),
  }

  static override flags = {
    destination: Flags.string({description: 'Arbitrum address to receive USDC (defaults to main wallet)', default: ''}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Withdraw)
    const amount = args.amount
    const isMainnet = true

    const creds = loadCredentials()
    if (!creds) {
      this.log(`  ${red('✘')} No wallet configured. Run: ${cyan('rift auth setup')}`)
      return
    }

    const destination = flags.destination || getAccountAddress(creds)

    // Check for existing WalletConnect session
    const session = await getExistingSession()
    if (!session) {
      this.log(`  ${red('✘')} No wallet session. Run: ${cyan('rift auth setup')} to reconnect.`)
      return
    }

    this.log('')
    this.log(`  Withdrawing $${amount} USDC to Arbitrum...`)
    this.log(`  ${dim('→ Approve in your wallet (check your phone)')}`)
    this.log('')

    const result = await requestWithdrawal(amount, destination, isMainnet)

    if (!result.success) {
      this.log(`  ${red('✘')} Withdrawal failed: ${result.error}`)
      return
    }

    // Post to Hyperliquid
    try {
      const response = await postToHyperliquid(result.action!, result.signature!, result.nonce!, isMainnet)
      this.log(`  ${green('✔')} Withdrawal submitted`)
      this.log(`  ${dim(`$${amount} USDC will arrive on Arbitrum in ~2 minutes`)}`)
      this.log(`  ${dim('$1 fee deducted by Hyperliquid')}`)
      this.log('')
    } catch (error: any) {
      this.log(`  ${red('✘')} Failed to submit withdrawal: ${error.message}`)
    }
  }
}
