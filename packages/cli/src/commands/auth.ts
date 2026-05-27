import {Flags, Args} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {generatePrivateKey, privateKeyToAccount} from 'viem/accounts'
import {createInterface} from 'node:readline'
import {saveCredentials, loadCredentials, hasCredentials, hasFullSetup, maskKey, maskAddress, getAccountAddress} from '../lib/credentials.js'
import {
  connectWallet, requestAgentApproval, requestBuilderFeeApproval,
  postApprovalToHyperliquid, disconnectWallet, showQRCode,
} from '../lib/walletconnect.js'
import {BUILDER_ADDRESS, BUILDER_FEE_DISPLAY, recordOnChainApproval, hasApprovedFees, approveFees} from '../lib/fees.js'
import type {Credentials} from '../lib/credentials.js'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => {
      rl.close()
      resolve(answer.trim())
    })
  })
}

export default class Auth extends GatedCommand {
  static override description = 'Set up wallet credentials for Hyperliquid live trading'

  static override examples = [
    '$ rift auth setup',
    '$ rift auth status',
    '$ rift auth reset',
  ]

  static override args = {
    action: Args.string({
      description: 'Action: setup, status, or reset',
      required: false,
    }),
  }

  static override flags = {}

  async run(): Promise<void> {
    const {args} = await this.parse(Auth)

    switch (args.action) {
      case 'setup':
        return this.walletConnectSetup()
      case 'status':
        return this.showStatus()
      case 'reset':
        return this.resetCredentials()
      default:
        // No action → show status or guide to setup
        if (hasFullSetup()) {
          return this.showStatus()
        }
        return this.walletConnectSetup()
    }
  }

  private showStatus(): void {
    const creds = loadCredentials()
    if (!creds) {
      this.log('')
      this.log(`  No credentials configured.`)
      this.log(`  Run: ${cyan('rift auth setup')}`)
      this.log('')
      return
    }

    this.log('')
    this.log(`  ${bold('RIFT Account Status')}`)
    this.log(`  ${dim('─'.repeat(45))}`)
    const mainWallet = getAccountAddress(creds)
    const agentOk = creds.agent_approved !== false  // absent = trust file
    const builderOk = creds.builder_fee_approved === true

    this.log(`  Main wallet:   ${mainWallet}`)
    this.log(`  API wallet:    ${creds.address || maskKey(creds.private_key)}`)
    this.log(`  Network:       ${creds.network}`)
    this.log(`  Agent:         ${agentOk ? green('✔ approved') : red('✘ not approved')}`)
    this.log(`  Builder fee:   ${builderOk ? green('✔ approved') : red('✘ not approved')}`)
    this.log(`  ${dim('─'.repeat(45))}`)

    if (!agentOk || !builderOk) {
      this.log(`  Run ${cyan('rift auth setup')} to complete setup.`)
    } else {
      this.log(`  ${green('Ready for live trading.')} Run: ${cyan('rift live')}`)
    }
    this.log('')
  }

  private async resetCredentials(): Promise<void> {
    const confirm = await ask(`\n  ${red('This will remove all credentials. Continue?')} ${dim('(yes/no)')}: `)
    if (confirm.toLowerCase() !== 'yes' && confirm.toLowerCase() !== 'y') {
      this.log(dim('\n  Cancelled.\n'))
      return
    }

    const credPath = (process.env.HOME || '~') + '/.rift/credentials.json'
    const fs = await import('node:fs')
    if (fs.existsSync(credPath)) {
      fs.unlinkSync(credPath)
    }
    this.log(`\n  ${green('✔')} Credentials removed. Run ${cyan('rift auth setup')} to set up again.\n`)
  }

  private async walletConnectSetup(): Promise<void> {
    const isMainnet = true

    this.log('')
    this.log(`  ${bold('╔═══════════════════════════════════════════╗')}`)
    this.log(`  ${bold('║          RIFT Account Setup               ║')}`)
    this.log(`  ${bold('╚═══════════════════════════════════════════╝')}`)
    this.log('')

    // Fee consent (if not already given)
    if (!hasApprovedFees()) {
      this.log(`  RIFT is ${bold('free')} for research, backtesting, and simulation.`)
      this.log(`  Live trading has a ${bold(BUILDER_FEE_DISPLAY)} builder fee per trade,`)
      this.log(`  collected on-chain by Hyperliquid to support RIFT development.`)
      this.log('')
      this.log(dim('  Example: $10,000 trade → $10 fee. Code is fully open-source.'))
      this.log('')

      const feeAnswer = await ask(`  ${cyan('Agree to continue?')} ${dim('(yes/no)')}: `)
      if (feeAnswer.toLowerCase() !== 'yes' && feeAnswer.toLowerCase() !== 'y') {
        this.log(`\n  ${dim('Setup cancelled.')}\n`)
        return
      }
      approveFees()
      this.log(`  ${green('✔')} Fee acknowledged`)
      this.log('')
    }

    this.log(dim('  Connect your Hyperliquid wallet to set up live trading.'))
    this.log(dim('  You\'ll approve two actions (one-time, from your phone).'))
    this.log('')

    // Step 1: Connect wallet via WalletConnect QR
    this.log(`  ${bold('Step 1:')} Connect your wallet`)
    this.log('')

    let session
    try {
      session = await connectWallet(
        (uri) => {
          this.log(`  Scan this QR code with your wallet app`)
          this.log(`  ${dim('(MetaMask, Rabby, Rainbow, Trust, etc.)')}`)
          this.log('')
          showQRCode(uri)
          this.log('')
          this.log(`  ${dim('Or paste this URI in your wallet:')}`)
          this.log(`  ${dim(uri.slice(0, 60) + '...')}`)
          this.log('')
          this.log(dim('  Waiting for connection...'))
        },
        (msg) => this.log(`  ${dim(msg)}`),
      )
    } catch (error: any) {
      this.log(`  ${red('✘')} Connection failed: ${error?.message || 'Unknown error'}`)
      this.log('')
      this.log(`  ${dim('Make sure your wallet app supports WalletConnect (Rabby, MetaMask Mobile, etc.)')}`)
      this.log(`  ${dim('Or paste your API wallet private key directly:')} ${cyan('rift auth setup --key 0x...')}`)
      this.log('')
      return
    }

    this.log(`  ${green('✔')} Connected: ${bold(session.account)}`)
    this.log('')

    // Step 2: Check Hyperliquid account
    this.log(`  ${bold('Step 2:')} Checking Hyperliquid account...`)

    // Mode-aware collateral read via shared helper (mirrors Python's
    // rift_data.account_mode.read_collateral so Python and TS surfaces
    // agree on the same wallet's numbers).
    let tradeable = 0
    let perpEquity = 0
    let spotUsdc = 0
    let mode: import('../lib/account-mode.js').AccountMode = 'unknown'
    try {
      const {readCollateral, hlBaseUrl} = await import('../lib/account-mode.js')
      const c = await readCollateral(hlBaseUrl(isMainnet), session.account)
      tradeable = c.total
      perpEquity = c.perpAccountValue
      spotUsdc = c.spotUsdc
      mode = c.mode
    } catch {
      // If we can't check, continue anyway — user might have geo-restrictions on info endpoint
    }

    if (tradeable > 0) {
      this.log(`  ${green('✔')} Hyperliquid account found (${mode} mode)`)
      this.log(`  ${green('✔')} Tradeable: $${tradeable.toLocaleString()} USDC${mode === 'standard' ? '' : `  ${dim(`(perp $${perpEquity.toFixed(2)} + spot $${spotUsdc.toFixed(2)})`)}`}`)
    } else {
      this.log(`  ${dim('!')} Could not verify balance (may need funds for live trading)`)
    }
    this.log('')

    // Step 3: Generate API wallet and approve agent
    this.log(`  ${bold('Step 3:')} Authorize RIFT to trade`)
    this.log('')
    this.log(dim('  RIFT will create a secure API wallet that can place'))
    this.log(dim('  trades on your behalf but CANNOT withdraw your funds.'))
    this.log('')

    // Generate a fresh API wallet keypair
    const apiKey = generatePrivateKey()
    const apiAccount = privateKeyToAccount(apiKey)
    const agentAddress = apiAccount.address

    this.log(dim('  Please approve in your wallet app...'))

    const agentResult = await requestAgentApproval(session, agentAddress, isMainnet)

    if (!agentResult.success) {
      this.log(`  ${red('✘')} Agent approval failed: ${agentResult.error}`)
      await disconnectWallet(session)
      return
    }

    // Post the signed approval to Hyperliquid (using the SAME nonce that was signed)
    try {
      await postApprovalToHyperliquid(agentResult.action!, agentResult.signature!, agentResult.nonce!, isMainnet)
      this.log(`  ${green('✔')} API wallet authorized`)
    } catch (error: any) {
      this.log(`  ${red('✘')} Failed to submit agent approval: ${error?.message}`)
      await disconnectWallet(session)
      return
    }

    this.log('')

    // Step 4: Approve builder fee
    this.log(`  ${bold('Step 4:')} Approve builder fee (${BUILDER_FEE_DISPLAY} per trade)`)
    this.log('')
    this.log(dim('  This supports RIFT development. Only applies to live trades.'))
    this.log(dim('  Backtesting and simulation are free.'))
    this.log('')
    this.log(dim('  Please approve in your wallet app...'))

    const builderResult = await requestBuilderFeeApproval(session, isMainnet)

    if (!builderResult.success) {
      this.log(`  ${red('✘')} Builder fee approval failed: ${builderResult.error}`)
      // Still save credentials — agent is approved, just builder fee isn't
      this.saveAndFinish(apiKey, agentAddress, session.account, isMainnet, true, false)
      await disconnectWallet(session)
      return
    }

    // Post the signed builder fee approval (using the SAME nonce that was signed)
    try {
      await postApprovalToHyperliquid(builderResult.action!, builderResult.signature!, builderResult.nonce!, isMainnet)
      this.log(`  ${green('✔')} Builder fee approved (${BUILDER_FEE_DISPLAY})`)
      recordOnChainApproval()
    } catch (error: any) {
      this.log(`  ${red('✘')} Failed to submit builder fee: ${error?.message}`)
      this.saveAndFinish(apiKey, agentAddress, session.account, isMainnet, true, false)
      await disconnectWallet(session)
      return
    }

    // Save everything and finish
    this.saveAndFinish(apiKey, agentAddress, session.account, isMainnet, true, true)

    // Session persists — do NOT disconnect. User can withdraw/transfer later without re-scanning.

    this.log('')
    this.log(`  ${bold('═'.repeat(50))}`)
    this.log('')
    this.log(`  ${green('✔ RIFT is ready.')}`)
    this.log('')
    this.log(`    Main wallet:  ${session.account}`)
    this.log(`    API wallet:   ${agentAddress}`)
    if (tradeable > 0) {
      this.log(`    Balance:      $${tradeable.toLocaleString()} USDC ${dim(`(${mode} mode)`)}`)
    }
    this.log(`    Builder fee:  ${BUILDER_FEE_DISPLAY} approved ${green('✔')}`)
    this.log(`    Network:      mainnet`)
    this.log('')
    this.log(`  ${dim('Your wallet session is saved — you won\'t need to scan again.')}`)
    this.log(`  ${dim('Trading happens silently via the API wallet.')}`)
    this.log(`  ${dim('Withdrawals and transfers send a notification to your phone.')}`)
    this.log('')
    this.log(`  ${bold('Next steps:')}`)
    this.log(`    ${cyan('rift guide')}              ${dim('— 9-step research-to-trade journey')}`)
    this.log(`    ${cyan('rift balance')}            ${dim('— check spot + perps balances')}`)
    this.log(`    ${cyan('rift algo --pair SUI')}    ${dim('— start algo trading')}`)
    this.log(`    ${cyan('rift buy HYPE --amount 10')} ${dim('— buy spot tokens')}`)
    this.log('')
    this.log(`  ${bold('═'.repeat(50))}`)
    this.log('')
  }

  private saveAndFinish(
    apiKey: string,
    apiAddress: string,
    mainAddress: string,
    _isMainnet: boolean,  // legacy positional, always true now
    agentApproved: boolean,
    builderApproved: boolean,
  ): void {
    const creds: Credentials = {
      private_key: apiKey,
      address: apiAddress,
      account_address: mainAddress,
      type: 'walletconnect',
      network: 'mainnet',
      registered_at: new Date().toISOString(),
      agent_approved: agentApproved,
      builder_fee_approved: builderApproved,
    }
    saveCredentials(creds)
  }
}
