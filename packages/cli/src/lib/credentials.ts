/**
 * Wallet credential management — canonical snake_case schema, matching
 * what the Python engine writes via `rift agent-pair` (rift_trade.api_wallet
 * → rift_core.keys.APIWalletKey, serialized via Pydantic).
 *
 * File location: ~/.rift/credentials   (no extension, matches Python)
 * Format: single-account JSON (snake_case fields).
 *
 * The loader is tolerant of two legacy formats:
 *   (a) ~/.rift/credentials.json with camelCase fields (old TS auth.ts writer)
 *   (b) Multi-account wrapped: {"default": {…}}
 * Both are migrated to the canonical form on first read.
 *
 * The Python engine is the authoritative writer. The TS `rift auth setup`
 * flow also writes to the same path + schema.
 */

import * as fs from 'node:fs'
import * as path from 'node:path'

export interface Credentials {
  // Always present (Python writer guarantees these)
  address: string                              // API wallet's on-chain address
  private_key: string                          // API wallet private key (64 hex chars, no 0x)
  network: 'mainnet'                           // mainnet-only post-testnet rip; field kept for schema continuity

  // Set by Python's APIWalletKey model
  name?: string                                // human-readable label
  registered_at?: string                       // ISO timestamp of pairing
  registered_tx?: string | null                // HL approveAgent tx hash

  // Set by the TS auth flow (or modern Python after the unification);
  // when absent, callers fall back to `address`.
  account_address?: string                     // Main wallet (the HL account being traded for)
  type?: 'api-wallet' | 'generated' | 'walletconnect'
  agent_approved?: boolean                     // ApproveAgent succeeded — implicit `true` when file exists
  builder_fee_approved?: boolean              // ApproveBuilderFee succeeded
}

const CRED_DIR = path.join(process.env.HOME || '~', '.rift')
const CANONICAL_PATH = path.join(CRED_DIR, 'credentials')
const LEGACY_PATH = path.join(CRED_DIR, 'credentials.json')


function ensureDir(): void {
  if (!fs.existsSync(CRED_DIR)) {
    fs.mkdirSync(CRED_DIR, {recursive: true, mode: 0o700})
  }
}


/** Map any of the known legacy formats onto the canonical snake_case shape. */
function migrate(data: Record<string, any>): Credentials | null {
  // Multi-account wrapper from old TS writer.
  if (data && typeof data === 'object' && 'default' in data
      && data.default && typeof data.default === 'object'
      && !('private_key' in data) && !('privateKey' in data)) {
    return migrate(data.default)
  }

  const private_key = data.private_key ?? data.privateKey
  const address = data.address ?? data.apiWalletAddress
  const network = data.network
  if (!private_key || !address) return null

  // Mainnet-only post-testnet rip. Any legacy testnet-paired wallet is
  // refused so the user gets a clear "re-pair on mainnet" message instead
  // of trades silently misrouting.
  if (network && network !== 'mainnet') return null

  return {
    address: String(address).toLowerCase(),
    private_key: String(private_key).replace(/^0x/, '').toLowerCase(),
    network: 'mainnet',
    name: data.name,
    registered_at: data.registered_at ?? data.createdAt,
    registered_tx: data.registered_tx ?? null,
    account_address: (data.account_address ?? data.accountAddress)?.toLowerCase(),
    type: data.type,
    agent_approved: data.agent_approved ?? data.agentApproved,
    builder_fee_approved: data.builder_fee_approved ?? data.builderFeeApproved,
  }
}


function readFile(filePath: string): Credentials | null {
  if (!fs.existsSync(filePath)) return null
  try {
    const raw = fs.readFileSync(filePath, 'utf-8')
    return migrate(JSON.parse(raw))
  } catch {
    return null
  }
}


export function loadCredentials(): Credentials | null {
  // Canonical path first — matches Python's writer.
  const canonical = readFile(CANONICAL_PATH)
  if (canonical) return canonical

  // Legacy TS path. If found, migrate it to the canonical location so
  // future reads don't keep migrating.
  const legacy = readFile(LEGACY_PATH)
  if (legacy) {
    try {
      saveCredentials(legacy)
    } catch {
      // Best effort — return what we read even if rewrite fails.
    }
    return legacy
  }

  return null
}


export function saveCredentials(creds: Credentials): void {
  ensureDir()
  // Atomic write: tmp + rename, with 0600 perms.
  const payload = JSON.stringify(creds, null, 2)
  const tmp = CANONICAL_PATH + '.tmp'
  fs.writeFileSync(tmp, payload, {mode: 0o600})
  fs.renameSync(tmp, CANONICAL_PATH)
  try {
    fs.chmodSync(CANONICAL_PATH, 0o600)
  } catch {
    // Permission tightening is best-effort.
  }
}


export function hasCredentials(): boolean {
  return loadCredentials() !== null
}


/**
 * Returns true if the wallet is ready for live trading (mainnet-only).
 *
 *   - File must exist with `address` + `private_key` (pairing complete).
 *   - `agent_approved` defaults to true when the file exists — the file is
 *     only written after a successful approveAgent transaction.
 *   - `builder_fee_approved` must be explicit `true`. Absent → user is
 *     prompted to run `rift approve-builder-fee` before their first trade.
 */
export function hasFullSetup(): boolean {
  const creds = loadCredentials()
  if (!creds) return false
  if (!creds.private_key || !creds.address) return false
  const agentOk = creds.agent_approved !== false  // absent = trust file
  return agentOk && creds.builder_fee_approved === true
}


/** Main wallet address — falls back to API wallet address when not stored
 * (older Python-paired wallets don't include account_address). */
export function getAccountAddress(creds: Credentials): string {
  return creds.account_address ?? creds.address
}


export function maskKey(key: string): string {
  if (key.length <= 10) return '****'
  return key.slice(0, 6) + '...' + key.slice(-4)
}


export function maskAddress(addr: string): string {
  if (addr.length <= 10) return addr
  return addr.slice(0, 6) + '...' + addr.slice(-4)
}
