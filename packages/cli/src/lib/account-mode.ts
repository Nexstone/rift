/**
 * Hyperliquid account abstraction mode detection + collateral reading.
 *
 * TS mirror of packages/data/src/rift_data/account_mode.py. The two MUST
 * agree on what counts as "tradeable collateral" per mode, or Python and
 * TS surfaces will show different numbers to the same user.
 *
 * Modes:
 *   - 'standard'         Spot and perp are separate. Only perp counts.
 *   - 'unified'          UI default. Spot USDC IS perp collateral.
 *   - 'portfolio_margin' Pooled USDC + LTV-weighted assets. Per HL docs,
 *                        perp dex user state is "not meaningful" here;
 *                        real collateral lives in spot. v0.1 counts USDC
 *                        only — non-USDC PM collateral (HYPE/BTC/USDH at
 *                        oracle*LTV) under-counted; LTV table not exposed
 *                        via info endpoints.
 *   - 'unknown'          Future HL mode we don't recognize. Treated as
 *                        unified (sum) for safety.
 *
 * Detection uses HL's /info endpoint with type='userAbstraction'. There's
 * a confusingly-named 'userDexAbstraction' endpoint — that's a DIFFERENT
 * mode (DEX Abstraction, being discontinued). Do not confuse.
 */

export type AccountMode = 'standard' | 'unified' | 'portfolio_margin' | 'unknown'

const HL_TO_FRIENDLY: Record<string, AccountMode> = {
  disabled: 'standard',
  unifiedAccount: 'unified',
  portfolioMargin: 'portfolio_margin',
}

export interface CollateralBreakdown {
  mode: AccountMode
  perpAccountValue: number
  perpMarginUsed: number
  perpAvailable: number
  spotUsdc: number
  /** What sizing logic / gates should use. */
  total: number
  /** True if spot USDC is NOT counted (Standard mode). */
  perpOnly: boolean
}

const _hlPost = async (baseUrl: string, body: unknown): Promise<any> => {
  const resp = await fetch(`${baseUrl}/info`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
  if (!resp.ok) throw new Error(`HL info ${resp.status}`)
  return resp.json()
}

export const hlBaseUrl = (_isMainnet: boolean = true): string =>
  'https://api.hyperliquid.xyz'

export async function queryAccountMode(baseUrl: string, address: string): Promise<AccountMode> {
  const raw = await _hlPost(baseUrl, {type: 'userAbstraction', user: address.toLowerCase()})
  if (typeof raw !== 'string') return 'unknown'
  return HL_TO_FRIENDLY[raw] ?? 'unknown'
}

/**
 * Read the wallet's available collateral, mode-aware. One source of truth
 * for "how much can this wallet trade with" in TS surfaces.
 *
 * Three HL info calls in parallel: perp state, spot state, mode.
 */
export async function readCollateral(baseUrl: string, address: string): Promise<CollateralBreakdown> {
  const addr = address.toLowerCase()
  const [perp, spot, modeRaw] = await Promise.all([
    _hlPost(baseUrl, {type: 'clearinghouseState', user: addr}),
    _hlPost(baseUrl, {type: 'spotClearinghouseState', user: addr}),
    _hlPost(baseUrl, {type: 'userAbstraction', user: addr}),
  ])

  const mode: AccountMode = typeof modeRaw === 'string' ? (HL_TO_FRIENDLY[modeRaw] ?? 'unknown') : 'unknown'
  const summary = perp?.marginSummary ?? {}
  const perpAccountValue = parseFloat(summary.accountValue ?? '0')
  const perpMarginUsed = parseFloat(summary.totalMarginUsed ?? '0')
  const perpAvailable = perpAccountValue - perpMarginUsed
  let spotUsdc = 0
  for (const b of spot?.balances ?? []) {
    if (b?.coin === 'USDC') { spotUsdc = parseFloat(b.total ?? '0'); break }
  }

  let total: number
  switch (mode) {
    case 'standard':
      total = perpAvailable
      break
    case 'unified':
    case 'portfolio_margin':
      // HL docs: under both modes, perp state is "not meaningful";
      // real collateral is in spot. v0.1 counts USDC only — see file
      // header for the PM non-USDC caveat.
      total = perpAvailable + spotUsdc
      break
    default:
      // unknown: sum, matching Python's safest-default behavior
      total = perpAvailable + spotUsdc
  }

  return {
    mode,
    perpAccountValue,
    perpMarginUsed,
    perpAvailable,
    spotUsdc,
    total,
    perpOnly: mode === 'standard',
  }
}
