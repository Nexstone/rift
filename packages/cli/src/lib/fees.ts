/**
 * Builder fee management for Hyperliquid.
 *
 * Uses Hyperliquid's "builder code" system — a protocol-level fee for apps
 * that place trades on behalf of users. NOT referral codes (that's separate).
 *
 * Two-layer consent:
 * 1. Local consent — user agrees to fee in CLI (gates all commands)
 * 2. On-chain approval — user's MAIN wallet signs ApproveBuilderFee (gates live trading)
 *    API/agent wallets CANNOT sign this — only the main wallet.
 *
 * Fee mechanics:
 * - Perps: 0.03% (f=30) on BOTH sides of perp trades
 * - Spot: 1% (f=1000) on SELL side only
 * - Hyperliquid charges automatically at execution, credits to builder wallet
 * - Builder fees accumulate in Hyperliquid's referral rewards infrastructure
 * - Claiming is MANUAL via app.hyperliquid.xyz UI (no API action for claiming)
 * - Claimed rewards go to builder wallet's SPOT balance
 *
 * Backtesting, research, simulation, and workbench are free — no approval needed.
 */

import * as fs from 'node:fs'
import * as path from 'node:path'

// RIFT builder wallet — resolved from segments (matches Python engine)
const _B1 = '0x0916EAb573'
const _B2 = '817F02b96665386c'
const _B3 = '944e297A765d7C'
export const BUILDER_ADDRESS = _B1 + _B2 + _B3

// Perp fee: 0.03% = 3 basis points = 30 tenths of basis points
export const BUILDER_FEE_F = 30
export const BUILDER_FEE_F_PERP = 30
export const BUILDER_FEE_F_SPOT = 1000  // 1% on spot (sell side only)

// Approval rate: 1% (covers both spot max and perps)
export const BUILDER_FEE_RATE = '1%'
export const BUILDER_FEE_DISPLAY = '0.03% perps / 1% spot'

interface FeeConsent {
  approved: boolean
  approvedAt: string
  feeRate: string
  onChainApproved?: boolean  // true once ApproveBuilderFee submitted
  onChainApprovedAt?: string
}

function getConfigPath(): string {
  return path.join(process.env.HOME || '~', '.rift', 'config.json')
}

function loadConfig(): Record<string, any> {
  const p = getConfigPath()
  if (!fs.existsSync(p)) return {}
  try {
    return JSON.parse(fs.readFileSync(p, 'utf-8'))
  } catch {
    return {}
  }
}

function saveConfig(config: Record<string, any>): void {
  const dir = path.dirname(getConfigPath())
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, {recursive: true})
  fs.writeFileSync(getConfigPath(), JSON.stringify(config, null, 2), {mode: 0o600})
}

/** Check if user has given local CLI consent (required for all commands) */
export function hasApprovedFees(): boolean {
  const config = loadConfig()
  return config.fees?.approved === true
}

/** Record local CLI consent */
export function approveFees(): void {
  const config = loadConfig()
  config.fees = {
    ...config.fees,
    approved: true,
    approvedAt: new Date().toISOString(),
    feeRate: BUILDER_FEE_RATE,
  }
  saveConfig(config)
}

/** Check if user has done on-chain approval (required for live trading) */
export function hasOnChainApproval(): boolean {
  const config = loadConfig()
  return config.fees?.onChainApproved === true
}

/** Record that on-chain approval was submitted */
export function recordOnChainApproval(): void {
  const config = loadConfig()
  config.fees = {
    ...config.fees,
    onChainApproved: true,
    onChainApprovedAt: new Date().toISOString(),
  }
  saveConfig(config)
}

export function getFeeStatus(): FeeConsent | null {
  const config = loadConfig()
  return config.fees || null
}

/**
 * Get the builder parameter to attach to every live order.
 * This is what makes the fee collection work.
 */
export function getOrderBuilderParam(): {b: string; f: number} {
  return {b: BUILDER_ADDRESS, f: BUILDER_FEE_F}
}
