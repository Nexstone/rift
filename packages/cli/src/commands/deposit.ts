import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {getExistingSession} from '../lib/walletconnect.js'
import {loadCredentials} from '../lib/credentials.js'
import {green, red, cyan, dim, bold} from '../lib/tui.js'

// Bridge contract address (mainnet)
const BRIDGE_MAINNET = '0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7'

// USDC contract address (mainnet)
const USDC_MAINNET = '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'

// Arbitrum mainnet chain ID
const ARB_MAINNET_CHAIN_ID = 42161

// Minimal ABI for batchedDepositWithPermit
const BRIDGE_ABI_FRAGMENT = [
  {
    inputs: [{
      components: [
        {name: 'user', type: 'address'},
        {name: 'usd', type: 'uint64'},
        {name: 'deadline', type: 'uint64'},
        {
          components: [
            {name: 'v', type: 'uint8'},
            {name: 'r', type: 'uint256'},
            {name: 's', type: 'uint256'},
          ],
          name: 'signature',
          type: 'tuple',
        },
      ],
      name: 'deposits',
      type: 'tuple[]',
    }],
    name: 'batchedDepositWithPermit',
    outputs: [],
    stateMutability: 'nonpayable',
    type: 'function',
  },
]

// Minimal ABI for USDC nonces query
const USDC_NONCES_ABI = [
  {
    inputs: [{name: 'owner', type: 'address'}],
    name: 'nonces',
    outputs: [{name: '', type: 'uint256'}],
    stateMutability: 'view',
    type: 'function',
  },
]

export default class Deposit extends GatedCommand {
  static override description = 'Deposit USDC from Arbitrum to Hyperliquid'

  static override examples = [
    '$ rift deposit 100',
  ]

  static override args = {
    amount: Args.string({description: 'USDC amount to deposit (minimum 5)', required: true}),
  }

  static override flags = {}

  async run(): Promise<void> {
    const {args} = await this.parse(Deposit)
    const amount = parseFloat(args.amount)

    if (amount < 5) {
      this.log(`  ${red('✘')} Minimum deposit is 5 USDC. Amounts below 5 are lost permanently.`)
      return
    }

    const creds = loadCredentials()
    if (!creds) {
      this.log(`  ${red('✘')} No wallet configured. Run: ${cyan('rift auth setup')}`)
      return
    }

    const session = await getExistingSession()
    if (!session) {
      this.log(`  ${red('✘')} No wallet session. Run: ${cyan('rift auth setup')} to reconnect.`)
      return
    }

    const walletAddress = session.account
    const bridgeAddress = BRIDGE_MAINNET
    const usdcAddress = USDC_MAINNET
    const chainId = ARB_MAINNET_CHAIN_ID
    const chainRef = `eip155:${chainId}`

    // Convert to raw USDC units (6 decimals)
    const rawAmount = BigInt(Math.round(amount * 1_000_000))
    const deadline = Math.floor(Date.now() / 1000) + 3600 // 1 hour from now

    this.log('')
    this.log(`  Depositing $${amount} USDC to Hyperliquid...`)
    this.log('')

    // Step 1: Get USDC nonce for the permit
    this.log(`  ${dim('Step 1/3: Querying USDC permit nonce...')}`)

    let nonce: number
    try {
      // Query USDC nonces via eth_call
      const nonceCallData = '0x7ecebe00' + walletAddress.slice(2).padStart(64, '0')
      const nonceResult = await session.client.request<string>({
        topic: session.topic,
        chainId: chainRef,
        request: {
          method: 'eth_call',
          params: [{to: usdcAddress, data: nonceCallData}, 'latest'],
        },
      })
      nonce = parseInt(nonceResult, 16)
    } catch {
      // Fallback: assume nonce 0 (first permit)
      nonce = 0
    }

    // Step 2: Sign USDC permit via WalletConnect
    this.log(`  ${dim('Step 2/3: Sign USDC permit...')}`)
    this.log(`  ${dim('→ Approve in your wallet (check your phone)')}`)

    const permitDomain = {
      name: 'USD Coin',
      version: '2',
      chainId,
      verifyingContract: usdcAddress,
    }

    const permitTypes = {
      EIP712Domain: [
        {name: 'name', type: 'string'},
        {name: 'version', type: 'string'},
        {name: 'chainId', type: 'uint256'},
        {name: 'verifyingContract', type: 'address'},
      ],
      Permit: [
        {name: 'owner', type: 'address'},
        {name: 'spender', type: 'address'},
        {name: 'value', type: 'uint256'},
        {name: 'nonce', type: 'uint256'},
        {name: 'deadline', type: 'uint256'},
      ],
    }

    const permitMessage = {
      owner: walletAddress,
      spender: bridgeAddress,
      value: rawAmount.toString(),
      nonce,
      deadline,
    }

    const permitTypedData = {
      domain: permitDomain,
      types: permitTypes,
      primaryType: 'Permit',
      message: permitMessage,
    }

    let permitSig: string
    try {
      permitSig = await session.client.request<string>({
        topic: session.topic,
        chainId: chainRef,
        request: {
          method: 'eth_signTypedData_v4',
          params: [walletAddress, JSON.stringify(permitTypedData)],
        },
      })
    } catch (error: any) {
      this.log(`  ${red('✘')} Permit signing failed: ${error?.message || 'User rejected'}`)
      return
    }

    this.log(`  ${green('✔')} Permit signed`)

    // Parse permit signature
    const sigHex = permitSig.startsWith('0x') ? permitSig.slice(2) : permitSig
    const permitR = '0x' + sigHex.slice(0, 64)
    const permitS = '0x' + sigHex.slice(64, 128)
    const permitV = parseInt(sigHex.slice(128, 130), 16)

    // Step 3: Send batchedDepositWithPermit transaction via WalletConnect
    this.log(`  ${dim('Step 3/3: Submit bridge deposit...')}`)
    this.log(`  ${dim('→ Approve transaction in your wallet')}`)

    // Encode batchedDepositWithPermit call data manually
    // Function selector: keccak256("batchedDepositWithPermit((address,uint64,uint64,(uint8,uint256,uint256))[])")
    // We'll use a simplified encoding approach

    // ABI encode the deposit struct array
    const encodedData = encodeBatchedDeposit(
      walletAddress,
      rawAmount,
      BigInt(deadline),
      permitV,
      permitR,
      permitS,
    )

    try {
      const txHash = await session.client.request<string>({
        topic: session.topic,
        chainId: chainRef,
        request: {
          method: 'eth_sendTransaction',
          params: [{
            from: walletAddress,
            to: bridgeAddress,
            data: encodedData,
            // Gas will be estimated by the wallet
          }],
        },
      })

      this.log(`  ${green('✔')} Deposit submitted!`)
      this.log(`  ${dim(`$${amount} USDC will arrive on Hyperliquid in ~1 minute`)}`)
      this.log(`  ${dim(`Tx: ${txHash}`)}`)
      this.log('')
      this.log(`  ${dim('Run:')} ${cyan('rift balance')} ${dim('to check when it arrives')}`)
      this.log('')
    } catch (error: any) {
      this.log(`  ${red('✘')} Deposit transaction failed: ${error?.message || 'User rejected'}`)
    }
  }
}

/**
 * ABI-encode the batchedDepositWithPermit call data.
 *
 * This encodes: batchedDepositWithPermit([(user, usd, deadline, (v, r, s))])
 */
function encodeBatchedDeposit(
  user: string,
  usd: bigint,
  deadline: bigint,
  v: number,
  r: string,
  s: string,
): string {
  // Function selector for batchedDepositWithPermit((address,uint64,uint64,(uint8,uint256,uint256))[])
  // Computed from keccak256 of the signature
  const selector = '0xea7fb094'

  // ABI encoding for dynamic array of structs
  const pad = (hex: string, bytes: number = 32) => hex.replace('0x', '').padStart(bytes * 2, '0')
  const toUint = (n: bigint | number) => pad(BigInt(n).toString(16))

  // Offset to array data (32 bytes)
  const arrayOffset = toUint(32n)
  // Array length (1 deposit)
  const arrayLength = toUint(1n)
  // Struct fields (packed, no dynamic types within struct)
  const userPadded = pad(user.toLowerCase())
  const usdPadded = toUint(usd)
  const deadlinePadded = toUint(deadline)
  const vPadded = toUint(BigInt(v))
  const rPadded = pad(r)
  const sPadded = pad(s)

  return selector + arrayOffset + arrayLength + userPadded + usdPadded + deadlinePadded + vPadded + rPadded + sPadded
}
