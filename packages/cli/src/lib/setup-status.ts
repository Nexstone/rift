/**
 * Phase 0 setup-status detector.
 *
 * Determines what state the user's RIFT installation is in by checking
 * file system state — cheap (just stat calls), runs on every CLI command
 * to drive the status footer and bare-rift wizard routing.
 *
 * No Python required; pure TS for speed (every CLI invocation reads this).
 */

import * as fs from 'node:fs'
import * as path from 'node:path'
import * as os from 'node:os'

const RIFT_HOME = path.join(os.homedir(), '.rift')
const KILL_FLAG = path.join(RIFT_HOME, 'KILL')
const ENV_FILE = path.join(RIFT_HOME, '.env')
const CREDENTIALS_FILE = path.join(RIFT_HOME, 'credentials')
const TOKENS_DIR = path.join(RIFT_HOME, 'tokens')

export type SetupState =
  | 'fresh'              // ~/.rift doesn't exist yet — first run
  | 'research-only'      // .env exists but no API wallet (user opted out of trading)
  | 'incomplete'         // partial setup — something interrupted
  | 'live-ready'         // API wallet registered, ready to trade
  | 'kill-active'        // global kill switch on
  | 'agent-revoked'      // API wallet present but chain-revoked (TODO: detect)

export interface SetupStatus {
  state: SetupState
  agentAddress: string | null
  network: 'mainnet' | null
  hasMainWalletPaired: boolean       // future: WC session present
  hasApiWallet: boolean
  hasEnvConfig: boolean
  tokenCount: number
  killSwitchActive: boolean
  riftVersion: string
}

const VERSION = '0.1.0'  // TODO: read from package.json

function safeExists(p: string): boolean {
  try {
    return fs.existsSync(p)
  } catch {
    return false
  }
}

function readApiWalletNetwork(): 'mainnet' | null {
  if (!safeExists(CREDENTIALS_FILE)) return null
  try {
    const data = JSON.parse(fs.readFileSync(CREDENTIALS_FILE, 'utf-8'))
    if (data?.network === 'mainnet') return 'mainnet'
  } catch {
    /* file unreadable or malformed; treat as missing */
  }
  return null
}

function readApiWalletAddress(): string | null {
  if (!safeExists(CREDENTIALS_FILE)) return null
  try {
    const data = JSON.parse(fs.readFileSync(CREDENTIALS_FILE, 'utf-8'))
    if (typeof data?.address === 'string') return data.address
  } catch {
    /* ignore */
  }
  return null
}

function countTokens(): number {
  if (!safeExists(TOKENS_DIR)) return 0
  try {
    return fs.readdirSync(TOKENS_DIR).filter(f => f.endsWith('.json')).length
  } catch {
    return 0
  }
}

/**
 * Inspect the local file system and return the current setup state.
 *
 * Pure function — no network, no Python invocation, no chain calls.
 * Safe to call from every CLI command (every footer render reads this).
 */
export function getSetupStatus(): SetupStatus {
  const killSwitchActive = safeExists(KILL_FLAG)
  const hasEnvConfig = safeExists(ENV_FILE)
  const hasApiWallet = safeExists(CREDENTIALS_FILE)
  const agentAddress = readApiWalletAddress()
  const network = readApiWalletNetwork()
  const tokenCount = countTokens()

  let state: SetupState
  if (killSwitchActive) {
    state = 'kill-active'
  } else if (!safeExists(RIFT_HOME)) {
    state = 'fresh'
  } else if (!hasApiWallet && hasEnvConfig) {
    state = 'research-only'
  } else if (!hasApiWallet) {
    // ~/.rift exists but nothing in it — interrupted setup
    state = 'incomplete'
  } else if (network === 'mainnet') {
    state = 'live-ready'
  } else {
    // wallet file present but no/invalid network field — likely a legacy
    // testnet-paired wallet that the loader now refuses.
    state = 'incomplete'
  }

  return {
    state,
    agentAddress,
    network,
    hasMainWalletPaired: false,  // wired up when WC integration ships (v0.2)
    hasApiWallet,
    hasEnvConfig,
    tokenCount,
    killSwitchActive,
    riftVersion: VERSION,
  }
}

/** Convenience: true iff the user can run live trading right now. */
export function canTradeNow(s: SetupStatus = getSetupStatus()): boolean {
  return s.state === 'live-ready' && !s.killSwitchActive
}

/** Convenience: short address ellipsis for display (0x1234…abcd). */
export function shortAddr(addr: string | null): string {
  if (!addr) return '—'
  if (addr.length < 14) return addr
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`
}
