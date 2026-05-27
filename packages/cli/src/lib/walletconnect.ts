/**
 * WalletConnect integration for wallet approvals and fund management.
 *
 * Used for:
 * 1. Auth setup — ApproveAgent + ApproveBuilderFee (one-time)
 * 2. Withdrawals — withdraw_from_bridge (occasional, needs main wallet)
 * 3. Transfers — usdClassTransfer spot↔perps (occasional, needs main wallet)
 * 4. Deposits — Arbitrum bridge interaction (occasional, needs main wallet)
 *
 * Sessions persist across CLI invocations via SQLite storage at ~/.rift/walletconnect.db.
 * Trading uses the agent key locally — no WalletConnect needed per trade.
 */

import * as os from 'node:os'
import * as path from 'node:path'
import SignClient from '@walletconnect/sign-client'
// @ts-ignore — no types available for qrcode-terminal
import qrcode from 'qrcode-terminal'
import {BUILDER_ADDRESS, BUILDER_FEE_RATE} from './fees.js'

// Register at cloud.walletconnect.com — free tier
const WALLETCONNECT_PROJECT_ID = '315aa4af2eda0390c8411a5b5e9b4f7a'

// Persistent session storage
const WC_DB_PATH = path.join(os.homedir(), '.rift', 'walletconnect.db')

// Hyperliquid EIP-712 constants
const HL_SIGNATURE_CHAIN_ID = '0x66eee'  // 421614 decimal
const HL_SIGNATURE_CHAIN_ID_DECIMAL = parseInt(HL_SIGNATURE_CHAIN_ID, 16)

// Hyperliquid API URL (mainnet-only post-testnet rip)
const HL_MAINNET_API = 'https://api.hyperliquid.xyz'

export interface ApprovalResult {
  success: boolean
  signature?: string
  nonce?: number
  action?: Record<string, any>
  error?: string
}

export interface WalletSession {
  client: SignClient
  topic: string
  account: string // user's main wallet address
}

// ─── SESSION MANAGEMENT ────────────────────────────────────

/**
 * Get or create the SignClient with persistent SQLite storage.
 * Sessions survive process restarts — no re-scan needed.
 */
async function getSignClient(): Promise<SignClient> {
  return SignClient.init({
    projectId: WALLETCONNECT_PROJECT_ID,
    metadata: {
      name: 'RIFT',
      description: 'Algorithmic trading engine for Hyperliquid',
      url: 'https://nexstone.io',
      icons: ['https://nexstone.io/nexstone-logo.png'],
    },
    storageOptions: {
      database: WC_DB_PATH,
    },
  })
}

/**
 * Resume an existing WalletConnect session from disk.
 * Returns null if no valid session exists (user needs to scan QR).
 */
export async function getExistingSession(): Promise<WalletSession | null> {
  try {
    const client = await getSignClient()
    const sessions = client.session.getAll()
    const now = Math.floor(Date.now() / 1000)
    const valid = sessions.find(s => s.expiry > now)

    if (!valid) return null

    // Auto-extend if expiring within 2 days
    const twoDays = 2 * 24 * 60 * 60
    if (valid.expiry - now < twoDays) {
      try {
        await client.extend({topic: valid.topic})
      } catch {
        // Extension failed — session might be dead
      }
    }

    // Verify connectivity
    try {
      await client.ping({topic: valid.topic})
    } catch {
      return null // session dead — user needs to re-scan
    }

    // Extract account address
    const accounts = valid.namespaces.eip155?.accounts || []
    const account = accounts.length > 0 ? accounts[0].split(':').pop()! : ''

    return {client, topic: valid.topic, account}
  } catch {
    return null
  }
}

/**
 * Connect a new wallet via QR code scan.
 * Session persists automatically via SQLite storage.
 */
export async function connectWallet(
  onQRCode: (uri: string) => void,
  onStatus: (msg: string) => void,
): Promise<WalletSession> {
  onStatus('Initializing WalletConnect...')

  const client = await getSignClient()

  const {uri, approval} = await client.connect({
    requiredNamespaces: {
      eip155: {
        methods: ['eth_signTypedData_v4', 'eth_signTypedData', 'eth_sendTransaction'],
        chains: ['eip155:42161'], // Arbitrum
        events: ['accountsChanged', 'chainChanged'],
      },
    },
  })

  if (!uri) {
    throw new Error('Failed to generate WalletConnect URI')
  }

  onQRCode(uri)

  const session = await approval()

  const accounts = session.namespaces.eip155?.accounts || []
  if (accounts.length === 0) {
    throw new Error('No accounts returned from wallet')
  }
  const account = accounts[0].split(':').pop()!

  return {client, topic: session.topic, account}
}

// ─── SIGNING REQUESTS ──────────────────────────────────────

/**
 * Request an EIP-712 signature via WalletConnect (persisted session).
 * Sends push notification to user's phone. Returns null if no session.
 */
export async function requestSignature(
  typedData: Record<string, any>,
): Promise<string | null> {
  const session = await getExistingSession()
  if (!session) return null

  try {
    const result = await session.client.request<string>({
      topic: session.topic,
      chainId: 'eip155:42161',
      request: {
        method: 'eth_signTypedData_v4',
        params: [session.account, JSON.stringify(typedData)],
      },
    })
    return result
  } catch {
    return null
  }
}

/**
 * Request ApproveAgent signature via WalletConnect.
 */
export async function requestAgentApproval(
  session: WalletSession,
  agentAddress: string,
  isMainnet: boolean = true,
): Promise<ApprovalResult> {
  const nonce = Date.now()
  const typedData = buildApproveAgentTypedData(agentAddress, nonce, isMainnet)

  const action = {
    type: 'approveAgent',
    hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
    signatureChainId: HL_SIGNATURE_CHAIN_ID,
    agentAddress,
    agentName: '',
    nonce,
  }

  try {
    const signature = await session.client.request<string>({
      topic: session.topic,
      chainId: 'eip155:42161',
      request: {
        method: 'eth_signTypedData_v4',
        params: [session.account, JSON.stringify(typedData)],
      },
    })
    return {success: true, signature, nonce, action}
  } catch (error: any) {
    return {success: false, error: error?.message || 'User rejected signature'}
  }
}

/**
 * Request ApproveBuilderFee signature via WalletConnect.
 */
export async function requestBuilderFeeApproval(
  session: WalletSession,
  isMainnet: boolean = true,
): Promise<ApprovalResult> {
  const nonce = Date.now()
  const typedData = buildApproveBuilderFeeTypedData(nonce, isMainnet)

  const action = {
    type: 'approveBuilderFee',
    hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
    signatureChainId: HL_SIGNATURE_CHAIN_ID,
    maxFeeRate: BUILDER_FEE_RATE,
    builder: BUILDER_ADDRESS,
    nonce,
  }

  try {
    const signature = await session.client.request<string>({
      topic: session.topic,
      chainId: 'eip155:42161',
      request: {
        method: 'eth_signTypedData_v4',
        params: [session.account, JSON.stringify(typedData)],
      },
    })
    return {success: true, signature, nonce, action}
  } catch (error: any) {
    return {success: false, error: error?.message || 'User rejected signature'}
  }
}

// ─── FUND MANAGEMENT SIGNING ───────────────────────────────

/**
 * Request withdrawal signature via WalletConnect.
 * User approves on phone, USDC bridges from HL to Arbitrum.
 */
export async function requestWithdrawal(
  amount: string,
  destination: string,
  isMainnet: boolean = true,
): Promise<ApprovalResult> {
  const nonce = Date.now()
  const typedData = buildWithdrawTypedData(destination, amount, nonce, isMainnet)

  const action = {
    type: 'withdraw3',
    hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
    signatureChainId: HL_SIGNATURE_CHAIN_ID,
    destination,
    amount,
    time: nonce,
  }

  const session = await getExistingSession()
  if (!session) {
    return {success: false, error: 'No wallet connected. Run: rift auth setup'}
  }

  try {
    const signature = await session.client.request<string>({
      topic: session.topic,
      chainId: 'eip155:42161',
      request: {
        method: 'eth_signTypedData_v4',
        params: [session.account, JSON.stringify(typedData)],
      },
    })
    return {success: true, signature, nonce, action}
  } catch (error: any) {
    return {success: false, error: error?.message || 'User rejected withdrawal'}
  }
}

/**
 * Request spot↔perps transfer signature via WalletConnect.
 */
export async function requestTransfer(
  amount: string,
  toPerp: boolean,
  isMainnet: boolean = true,
): Promise<ApprovalResult> {
  const nonce = Date.now()
  const typedData = buildTransferTypedData(amount, toPerp, nonce, isMainnet)

  const action = {
    type: 'usdClassTransfer',
    hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
    signatureChainId: HL_SIGNATURE_CHAIN_ID,
    amount,
    toPerp,
    nonce,
  }

  const session = await getExistingSession()
  if (!session) {
    return {success: false, error: 'No wallet connected. Run: rift auth setup'}
  }

  try {
    const signature = await session.client.request<string>({
      topic: session.topic,
      chainId: 'eip155:42161',
      request: {
        method: 'eth_signTypedData_v4',
        params: [session.account, JSON.stringify(typedData)],
      },
    })
    return {success: true, signature, nonce, action}
  } catch (error: any) {
    return {success: false, error: error?.message || 'User rejected transfer'}
  }
}

// ─── POST TO HYPERLIQUID ───────────────────────────────────

/**
 * Post a signed action to Hyperliquid's exchange API.
 */
export async function postToHyperliquid(
  action: Record<string, any>,
  signature: string,
  nonce: number,
  isMainnet: boolean = true,
): Promise<Record<string, any>> {
  const baseUrl = HL_MAINNET_API

  // Parse signature into r, s, v
  const sigBytes = signature.startsWith('0x') ? signature.slice(2) : signature
  const r = '0x' + sigBytes.slice(0, 64)
  const s = '0x' + sigBytes.slice(64, 128)
  const v = parseInt(sigBytes.slice(128, 130), 16)

  const response = await fetch(`${baseUrl}/exchange`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      action,
      nonce,
      signature: {r, s, v},
    }),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(`Hyperliquid API error: ${response.status} — ${text}`)
  }

  return response.json() as Promise<Record<string, any>>
}

// Legacy alias
export const postApprovalToHyperliquid = postToHyperliquid

/**
 * Display QR code in terminal.
 */
export function showQRCode(uri: string): void {
  qrcode.generate(uri, {small: true})
}

/**
 * Disconnect WalletConnect session (only if user explicitly requests).
 */
export async function disconnectWallet(session: WalletSession): Promise<void> {
  try {
    await session.client.disconnect({
      topic: session.topic,
      reason: {code: 6000, message: 'User disconnected'},
    })
  } catch {
    // Ignore disconnect errors
  }
}

// ─── EIP-712 TYPE BUILDERS ─────────────────────────────────

const HL_DOMAIN = {
  name: 'HyperliquidSignTransaction',
  version: '1',
  chainId: HL_SIGNATURE_CHAIN_ID_DECIMAL,
  verifyingContract: '0x0000000000000000000000000000000000000000',
}

const EIP712_DOMAIN_TYPE = [
  {name: 'name', type: 'string'},
  {name: 'version', type: 'string'},
  {name: 'chainId', type: 'uint256'},
  {name: 'verifyingContract', type: 'address'},
]

function buildApproveAgentTypedData(agentAddress: string, nonce: number, isMainnet: boolean) {
  return {
    domain: HL_DOMAIN,
    types: {
      EIP712Domain: EIP712_DOMAIN_TYPE,
      'HyperliquidTransaction:ApproveAgent': [
        {name: 'hyperliquidChain', type: 'string'},
        {name: 'agentAddress', type: 'address'},
        {name: 'agentName', type: 'string'},
        {name: 'nonce', type: 'uint64'},
      ],
    },
    primaryType: 'HyperliquidTransaction:ApproveAgent',
    message: {
      hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
      agentAddress,
      agentName: '',
      nonce,
    },
  }
}

function buildApproveBuilderFeeTypedData(nonce: number, isMainnet: boolean) {
  return {
    domain: HL_DOMAIN,
    types: {
      EIP712Domain: EIP712_DOMAIN_TYPE,
      'HyperliquidTransaction:ApproveBuilderFee': [
        {name: 'hyperliquidChain', type: 'string'},
        {name: 'maxFeeRate', type: 'string'},
        {name: 'builder', type: 'address'},
        {name: 'nonce', type: 'uint64'},
      ],
    },
    primaryType: 'HyperliquidTransaction:ApproveBuilderFee',
    message: {
      hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
      maxFeeRate: BUILDER_FEE_RATE,
      builder: BUILDER_ADDRESS,
      nonce,
    },
  }
}

function buildWithdrawTypedData(destination: string, amount: string, nonce: number, isMainnet: boolean) {
  return {
    domain: HL_DOMAIN,
    types: {
      EIP712Domain: EIP712_DOMAIN_TYPE,
      'HyperliquidTransaction:Withdraw': [
        {name: 'hyperliquidChain', type: 'string'},
        {name: 'destination', type: 'string'},
        {name: 'amount', type: 'string'},
        {name: 'time', type: 'uint64'},
      ],
    },
    primaryType: 'HyperliquidTransaction:Withdraw',
    message: {
      hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
      destination,
      amount,
      time: nonce,
    },
  }
}

function buildTransferTypedData(amount: string, toPerp: boolean, nonce: number, isMainnet: boolean) {
  return {
    domain: HL_DOMAIN,
    types: {
      EIP712Domain: EIP712_DOMAIN_TYPE,
      'HyperliquidTransaction:UsdClassTransfer': [
        {name: 'hyperliquidChain', type: 'string'},
        {name: 'amount', type: 'string'},
        {name: 'toPerp', type: 'bool'},
        {name: 'nonce', type: 'uint64'},
      ],
    },
    primaryType: 'HyperliquidTransaction:UsdClassTransfer',
    message: {
      hyperliquidChain: isMainnet ? 'Mainnet' : 'Testnet',
      amount,
      toPerp,
      nonce,
    },
  }
}
