/**
 * Shared TUI (Terminal UI) helpers for RIFT.
 *
 * Single source of truth for ANSI colors, box drawing, padding,
 * and display utilities. Every command imports from here.
 */

// ─── ANSI Colors ─────────────────────────────────────────────

export const green = (s: string) => `\x1b[32m${s}\x1b[0m`
export const red = (s: string) => `\x1b[31m${s}\x1b[0m`
export const yellow = (s: string) => `\x1b[33m${s}\x1b[0m`
export const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`
export const blue = (s: string) => `\x1b[34m${s}\x1b[0m`
export const magenta = (s: string) => `\x1b[35m${s}\x1b[0m`
export const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
export const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
export const italic = (s: string) => `\x1b[3m${s}\x1b[0m`
export const underline = (s: string) => `\x1b[4m${s}\x1b[0m`

// Backgrounds
export const greenBg = (s: string) => `\x1b[42m\x1b[30m${s}\x1b[0m`
export const redBg = (s: string) => `\x1b[41m\x1b[97m${s}\x1b[0m`
export const yellowBg = (s: string) => `\x1b[43m\x1b[30m${s}\x1b[0m`
export const cyanBg = (s: string) => `\x1b[46m\x1b[30m${s}\x1b[0m`
export const blueBg = (s: string) => `\x1b[44m\x1b[97m${s}\x1b[0m`

// ─── ANSI Measurement ────────────────────────────────────────

/** Strip all ANSI escape codes to get raw visible text */
export function stripAnsi(s: string): string {
  return s.replace(/\x1b\[[0-9;]*m/g, '')
}

/** Get visible character count (excluding ANSI codes) */
export function visLen(s: string): number {
  return stripAnsi(s).length
}

/** Pad end to visible width (ANSI-aware) */
export function padEndVis(s: string, width: number): string {
  return s + ' '.repeat(Math.max(0, width - visLen(s)))
}

/** Pad start to visible width (ANSI-aware) */
export function padStartVis(s: string, width: number): string {
  return ' '.repeat(Math.max(0, width - visLen(s))) + s
}

// ─── Value Formatters ────────────────────────────────────────

/** Color a P&L value: green if positive, red if negative, yellow if zero */
export function colorPnl(val: number, suffix = ''): string {
  const str = `${val >= 0 ? '+' : ''}${val}${suffix}`
  if (val > 0) return green(str)
  if (val < 0) return red(str)
  return yellow(str)
}

/** Color a number: green if positive, red if negative, yellow if zero */
export function colorNum(val: number, suffix = ''): string {
  const str = `${val}${suffix}`
  if (val > 0) return green(str)
  if (val < 0) return red(str)
  return yellow(str)
}

/** Color a grade: A=green, B=cyan, C=yellow, D/F=red */
export function gradeColor(grade: string): string {
  if (grade === 'A') return green(bold(grade))
  if (grade === 'B') return cyan(bold(grade))
  if (grade === 'C') return yellow(bold(grade))
  return red(bold(grade))
}

/** Visual bar chart */
export function bar(value: number, max: number, width: number = 10): string {
  const filled = Math.round(Math.max(0, Math.min(1, value / max)) * width)
  const empty = width - filled
  return green('█'.repeat(filled)) + dim('░'.repeat(empty))
}

/** Sparkline chart from price history */
export function sparkline(prices: number[], width: number = 24): string {
  if (prices.length < 2) return dim('─'.repeat(width))
  let min = Infinity
  let max = -Infinity
  for (const p of prices) {
    if (p < min) min = p
    if (p > max) max = p
  }
  const range = max - min || 1
  const blocks = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']
  const step = Math.max(1, Math.floor(prices.length / width))
  const sampled: number[] = []
  for (let i = 0; i < prices.length; i += step) sampled.push(prices[i])
  let result = ''
  for (const p of sampled.slice(-width)) {
    const idx = Math.min(blocks.length - 1, Math.floor(((p - min) / range) * (blocks.length - 1)))
    result += (p >= prices[0]) ? green(blocks[idx]) : red(blocks[idx])
  }
  return result
}

/** Stop loss proximity bar */
export function proximityBar(proximity: number, width: number = 30): string {
  if (proximity <= 0) return ''
  const clamped = Math.min(1.0, Math.max(0, proximity))
  const filled = Math.round(clamped * width)
  const empty = width - filled
  if (clamped > 0.8) return redBg('█'.repeat(filled)) + dim('░'.repeat(empty))
  if (clamped > 0.5) return yellow('█'.repeat(filled)) + dim('░'.repeat(empty))
  return dim('█'.repeat(filled) + '░'.repeat(empty))
}

/** Funding countdown display */
export function fundingCountdown(minutes: number, predicted: number): string {
  if (minutes <= 0) return ''
  const timeStr = minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
  const predStr = predicted > 0
    ? green(`+$${predicted.toFixed(2)}`)
    : predicted < 0
      ? red(`-$${Math.abs(predicted).toFixed(2)}`)
      : dim('$0.00')
  return minutes <= 5 ? `${yellow('⏱')} ${bold(timeStr)} → ${predStr}` : `${dim('⏱')} ${timeStr} → ${predStr}`
}

/** Mask an address for display: 0xABC...1234 */
export function maskAddress(addr: string): string {
  if (addr.length <= 10) return addr
  return addr.slice(0, 6) + '...' + addr.slice(-4)
}

// ─── Box Drawing ─────────────────────────────────────────────

/**
 * Create a box row helper for a given inner width.
 * Returns a function that takes content and produces a padded row.
 *
 * Usage:
 *   const row = boxRow(56)
 *   console.log(row('  Hello world'))
 *   // Output: "  │  Hello world                                     │"
 */
export function boxRow(innerWidth: number, border = dim('│')): (content: string) => string {
  return (content: string) => {
    const pad = Math.max(0, innerWidth - visLen(content))
    return `  ${border} ${content}${' '.repeat(pad)}${border}`
  }
}

/** Create a bold box row (for headers, live trading) */
export function boldBoxRow(innerWidth: number): (content: string) => string {
  return (content: string) => {
    const pad = Math.max(0, innerWidth - visLen(content))
    return `  ${bold('║')}${content}${' '.repeat(pad)}${bold('║')}`
  }
}

/** Box top border */
export function boxTop(innerWidth: number): string {
  return `  ${dim('┌' + '─'.repeat(innerWidth + 1) + '┐')}`
}

/** Box bottom border */
export function boxBottom(innerWidth: number): string {
  return `  ${dim('└' + '─'.repeat(innerWidth + 1) + '┘')}`
}

/** Box divider */
export function boxDivider(innerWidth: number): string {
  return `  ${dim('├' + '─'.repeat(innerWidth + 1) + '┤')}`
}

/** Bold box top */
export function boldBoxTop(innerWidth: number): string {
  return `  ${bold('╔' + '═'.repeat(innerWidth) + '╗')}`
}

/** Bold box bottom */
export function boldBoxBottom(innerWidth: number): string {
  return `  ${bold('╚' + '═'.repeat(innerWidth) + '╝')}`
}

/** Bold box divider */
export function boldBoxDivider(innerWidth: number): string {
  return `  ${bold('╠' + '═'.repeat(innerWidth) + '╣')}`
}

/**
 * Create a right-aligned result row: "  Label:           value"
 */
export function resultRow(innerWidth: number, border = dim('│')): (label: string, value: string) => string {
  return (label: string, value: string) => {
    const labelPart = `  ${label}:`
    const gap = Math.max(1, innerWidth - labelPart.length - visLen(value) - 1)
    const pad = Math.max(0, innerWidth - labelPart.length - gap - visLen(value))
    return `  ${border} ${labelPart}${' '.repeat(gap)}${value}${' '.repeat(pad)}${border}`
  }
}

// ─── Input ───────────────────────────────────────────────────

import {createInterface} from 'node:readline'

/** Prompt user for input */
export function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => {
      rl.close()
      resolve(answer.trim())
    })
  })
}

// ─── Word Wrap ───────────────────────────────────────────────

/** Wrap text to fit within maxWidth characters */
export function wordWrap(text: string, maxWidth: number): string[] {
  const words = text.split(' ')
  const lines: string[] = []
  let current = ''
  for (const word of words) {
    if (current.length + word.length + 1 > maxWidth) {
      lines.push(current)
      current = word
    } else {
      current = current ? current + ' ' + word : word
    }
  }
  if (current) lines.push(current)
  return lines
}

// ─── Dashboard State (Reducer Pattern) ───────────────────────

/**
 * Accumulates data from heartbeats, candle events, and local ticks.
 * The renderer always reads from this — data never disappears
 * because a different event type arrived.
 */
export interface DashboardState {
  // From any event (heartbeat or candle)
  price: number
  priceDelta: number
  sessionHigh: number
  sessionLow: number
  equity: number
  totalEquity: number
  totalPnlPct: number
  unrealizedPnl: number
  numTrades: number
  winRate: number
  totalFunding: number
  fundingRate: number
  fundingCountdownMin: number
  predictedFunding: number
  position: Record<string, any> | null
  stopProximity: number
  priceHistory: number[]
  recentTrades: Array<Record<string, any>>
  strategy: string
  pair: string
  interval: string

  // From candle events only (persists between heartbeats)
  reasoning: Record<string, any> | null
  conditions: Array<Record<string, any>>
  signalStatus: string
  summary: string
  recentCandles: Array<Record<string, any>>
  indicators: Record<string, number>

  // Candle countdown (baseline from Python, interpolated locally)
  candleRemainingBaseline: number
  candleProgressBaseline: number
  heartbeatTime: number  // Date.now() when last heartbeat arrived

  // Extras
  peakEquity: number
  wallet: string  // for live mode
  isLive: boolean
}

export function createDashboardState(): DashboardState {
  return {
    price: 0, priceDelta: 0, sessionHigh: 0, sessionLow: 0,
    equity: 0, totalEquity: 0, totalPnlPct: 0, unrealizedPnl: 0,
    numTrades: 0, winRate: 0, totalFunding: 0, fundingRate: 0,
    fundingCountdownMin: 0, predictedFunding: 0,
    position: null, stopProximity: 0,
    priceHistory: [], recentTrades: [],
    strategy: '', pair: '', interval: '',
    reasoning: null, conditions: [], signalStatus: 'waiting', summary: '',
    recentCandles: [], indicators: {},
    candleRemainingBaseline: 0, candleProgressBaseline: 0, heartbeatTime: 0,
    peakEquity: 0, wallet: '', isLive: false,
  }
}

/**
 * Merge incoming engine message into dashboard state.
 * Heartbeats update price/equity/funding. Candle events also update reasoning/candles.
 */
export function updateDashboardState(
  ds: DashboardState,
  state: Record<string, any>,
  msg: Record<string, any>,
  msgType: string,
): void {
  // Always update from state dict (present in both heartbeat and candle)
  ds.price = (state.last_price as number) ?? ds.price
  ds.priceDelta = (state.price_delta as number) ?? 0
  ds.sessionHigh = (state.session_high as number) ?? ds.sessionHigh
  ds.sessionLow = (state.session_low as number) ?? ds.sessionLow
  ds.equity = (state.equity as number) ?? ds.equity
  ds.totalEquity = (state.total_equity as number) ?? ds.totalEquity
  ds.totalPnlPct = (state.total_pnl_pct as number) ?? ds.totalPnlPct
  ds.unrealizedPnl = (state.unrealized_pnl as number) ?? ds.unrealizedPnl
  ds.numTrades = (state.num_trades as number) ?? ds.numTrades
  ds.winRate = (state.win_rate as number) ?? ds.winRate
  ds.totalFunding = (state.total_funding as number) ?? ds.totalFunding
  ds.fundingRate = (state.last_funding_rate as number) ?? ds.fundingRate
  ds.position = (state.position as Record<string, any>) ?? ds.position
  ds.stopProximity = (state.stop_proximity as number) ?? ds.stopProximity
  ds.priceHistory = (state.price_history as number[]) ?? ds.priceHistory
  ds.recentTrades = (state.recent_trades as Array<Record<string, any>>) ?? ds.recentTrades
  ds.strategy = (state.strategy as string) ?? ds.strategy
  ds.pair = (state.pair as string) ?? ds.pair
  ds.interval = (state.interval as string) ?? ds.interval
  ds.peakEquity = (state.peak_equity as number) ?? ds.peakEquity
  ds.wallet = (state.wallet as string) ?? ds.wallet
  ds.isLive = (state.is_live as boolean) ?? ds.isLive

  // Always update from msg
  ds.fundingCountdownMin = (msg.funding_countdown_min as number) ?? ds.fundingCountdownMin
  ds.predictedFunding = (msg.predicted_funding as number) ?? ds.predictedFunding

  // Candle countdown baseline
  ds.candleRemainingBaseline = (msg.candle_remaining_sec as number) ?? ds.candleRemainingBaseline
  ds.candleProgressBaseline = (msg.candle_progress as number) ?? ds.candleProgressBaseline
  ds.heartbeatTime = Date.now()

  // Reasoning + indicators: update from both candle and heartbeat when present
  const msgReasoning = msg.reasoning as Record<string, any> | undefined
  if (msgReasoning) {
    ds.reasoning = msgReasoning
    ds.conditions = (msgReasoning.conditions as Array<Record<string, any>>) ?? ds.conditions
    ds.signalStatus = (msgReasoning.signal_status as string) ?? ds.signalStatus
    ds.summary = (msgReasoning.summary as string) ?? ds.summary
  }
  const msgIndicators = msg.indicators as Record<string, number> | undefined
  if (msgIndicators) {
    ds.indicators = msgIndicators
  }

  // Recent candles from state (present in both but only grows on candle events)
  const rc = state.recent_candles as Array<Record<string, any>>
  if (rc && rc.length > 0) {
    ds.recentCandles = rc
  }
}

/**
 * Get interpolated candle countdown from local clock.
 */
export function getLocalCountdown(ds: DashboardState): {remaining: number; progress: number} {
  const secSince = Math.floor((Date.now() - ds.heartbeatTime) / 1000)
  const remaining = Math.max(0, ds.candleRemainingBaseline - secSince)
  const totalSec = ds.candleProgressBaseline > 0 && ds.candleProgressBaseline < 1
    ? Math.round(ds.candleRemainingBaseline / (1 - ds.candleProgressBaseline))
    : 3600
  const progress = totalSec > 0 ? Math.min(1, 1 - remaining / totalSec) : 0
  return {remaining, progress}
}
