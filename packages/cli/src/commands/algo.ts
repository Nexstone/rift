import {Flags, Args} from '@oclif/core'
import {createInterface} from 'node:readline'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {GatedCommand} from '../lib/base-command.js'
import {spawnDaemon, runEngine, getAlgoSessionsDir, getAlgoPidsDir} from '../lib/python-bridge.js'
import {loadCredentials, hasFullSetup, maskAddress, getAccountAddress} from '../lib/credentials.js'
import {BUILDER_FEE_DISPLAY} from '../lib/fees.js'
import type {EngineMessage} from '../lib/python-bridge.js'
import {
  green, red, yellow, cyan, bold, dim, greenBg, redBg,
  visLen, padEndVis, colorPnl,
  sparkline, proximityBar, fundingCountdown, maskAddress as maskAddr,
  createDashboardState, getLocalCountdown,
} from '../lib/tui.js'
import type {DashboardState} from '../lib/tui.js'

function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => { rl.close(); resolve(answer.trim()) })
  })
}

function sessionKey(strategy: string, pair: string): string {
  const coin = pair.replace(/-PERP/i, '').toUpperCase()
  return `${strategy}_${coin}`
}

function isSessionRunning(key: string): boolean {
  const pidFile = path.join(getAlgoPidsDir(), `${key}.pid`)
  if (!fs.existsSync(pidFile)) return false
  try {
    const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
    process.kill(pid, 0) // test if alive
    return true
  } catch {
    // Stale PID — clean up
    try { fs.unlinkSync(pidFile) } catch {}
    return false
  }
}

function readSessionSnapshot(key: string): Record<string, any> | null {
  const stateFile = path.join(getAlgoSessionsDir(), `${key}.json`)
  if (!fs.existsSync(stateFile)) return null
  try {
    return JSON.parse(fs.readFileSync(stateFile, 'utf-8'))
  } catch {
    return null
  }
}

// ─── COMMAND ───

export default class Algo extends GatedCommand {
  static override description = 'Algo trading — run automated strategies on Hyperliquid with real orders'

  static override examples = [
    '$ rift algo trend_follow --pair BTC --tf 4h',
    '$ rift algo status',
    '$ rift algo stop',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name, or "status"/"stop"', required: false}),
  }

  static override flags = {
    pair: Flags.string({description: 'Ticker symbol (e.g. BTC, ETH, SOL)', default: 'BTC'}),
    tf: Flags.string({description: 'Timeframe'}),
    equity: Flags.integer({description: 'Starting equity (0 = auto)', default: 0}),
    all: Flags.boolean({description: 'Stop all running sessions', default: false}),
  }

  private sessionStart = 0
  private dashboardActive = false
  private tickTimer: ReturnType<typeof setInterval> | null = null
  private ds: DashboardState = createDashboardState()
  private viewerRunning = false

  private static readonly DASHBOARD_HEIGHT = 17

  private clearDashboard(): void {
    if (this.dashboardActive) {
      const h = Algo.DASHBOARD_HEIGHT
      process.stdout.write(`\x1b[${h}A`)
      for (let i = 0; i < h; i++) process.stdout.write('\x1b[2K\n')
      process.stdout.write(`\x1b[${h}A`)
      this.dashboardActive = false
    }
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Algo)

    // ─── Subcommands ───
    if (args.strategy === 'status') {
      await this.showStatus()
      return
    }
    if (args.strategy === 'stop') {
      await this.stopSession(flags.pair!, flags.all)
      return
    }

    // ─── Pre-flight checks ───

    if (!hasFullSetup()) {
      this.log('')
      this.log(`  ${red('✘')} Algo trading requires account setup.`)
      this.log(`  Run: ${cyan('rift auth setup')}`)
      this.log('')
      const doSetup = await ask(`  ${cyan('Run setup now?')} ${dim('(yes/no)')}: `)
      if (doSetup.toLowerCase() === 'yes' || doSetup.toLowerCase() === 'y') {
        await this.config.runCommand('auth', ['setup'])
        if (!hasFullSetup()) {
          this.log(`\n  ${red('✘')} Setup incomplete. Try again.\n`)
          return
        }
      } else {
        return
      }
    }

    const creds = loadCredentials()
    if (!creds) {
      this.log(`\n  ${red('✘')} No credentials. Run: ${cyan('rift auth setup')}\n`)
      return
    }

    // Strategy selection
    let strategy = args.strategy
    if (!strategy) {
      strategy = await this.interactiveStrategyPicker()
      if (!strategy) return
    }

    const key = sessionKey(strategy, flags.pair!)

    // If already running, attach the viewer
    if (isSessionRunning(key)) {
      this.log('')
      this.log(`  ${greenBg(' ● ALGO ')} Session already running: ${bold(strategy)} on ${flags.pair!}`)
      this.log(`  ${dim('Attaching dashboard viewer... Press Ctrl+C to detach (trading continues).')}`)
      this.log('')
      await this.attachViewer(key)
      return
    }

    // Risk disclaimer + confirmation gate
    this.log('')
    this.log(`  ${bold('╔══════════════════════════════════════════════════════════════╗')}`)
    this.log(`  ${bold('║')}  ${greenBg(' ● ALGO ')} ${bold('REAL MONEY — REAL ORDERS')}                          ${bold('║')}`)
    this.log(`  ${bold('╠══════════════════════════════════════════════════════════════╣')}`)
    const bw = 62
    const cRow = (content: string) => {
      const pad = Math.max(0, bw - visLen(content))
      return `  ${bold('║')}${content}${' '.repeat(pad)}${bold('║')}`
    }
    this.log(cRow(`  Strategy:  ${bold(strategy)}`))
    this.log(cRow(`  Pair:      ${flags.pair!} PERP`))
    this.log(cRow(`  Wallet:    ${maskAddress(getAccountAddress(creds))}`))
    this.log(cRow(`  Fee:       ${BUILDER_FEE_DISPLAY} per side`))
    this.log(`  ${bold('╠══════════════════════════════════════════════════════════════╣')}`)
    this.log(cRow(`  ${red('RISK DISCLAIMER')}`))
    this.log(cRow(``))
    this.log(cRow(`  ${dim('Trading perpetual futures involves substantial risk of')}`))
    this.log(cRow(`  ${dim('loss. Past performance and backtested results do NOT')}`))
    this.log(cRow(`  ${dim('guarantee future returns. You may lose some or all of')}`))
    this.log(cRow(`  ${dim('your deposited funds.')}`))
    this.log(cRow(``))
    this.log(cRow(`  ${dim('RIFT is provided "as is" without warranty of any kind.')}`))
    this.log(cRow(`  ${dim('Nexstone and its contributors are NOT liable for any')}`))
    this.log(cRow(`  ${dim('trading losses, missed executions, software errors, or')}`))
    this.log(cRow(`  ${dim('exchange outages. You are solely responsible for your')}`))
    this.log(cRow(`  ${dim('trading decisions and capital.')}`))
    this.log(cRow(``))
    this.log(cRow(`  ${dim('By typing "LIVE" you acknowledge these risks and agree')}`))
    this.log(cRow(`  ${dim('that you trade entirely at your own risk.')}`))
    this.log(`  ${bold('╚══════════════════════════════════════════════════════════════╝')}`)
    this.log('')

    const confirm = await ask(`  ${red('Type "LIVE" to accept risk & start')}: `)
    if (confirm !== 'LIVE') {
      this.log(dim('\n  Cancelled.\n'))
      return
    }

    this.log('')
    await this.startAlgo(strategy, flags.pair!, flags.tf, flags.equity!, creds)
  }

  private async interactiveStrategyPicker(): Promise<string | undefined> {
    // Discover strategies dynamically from engine
    let strategies: {name: string; desc: string; grade: string}[] = []
    try {
      await new Promise<void>((resolve, reject) => {
        runEngine('strategies', [], (msg: EngineMessage) => {
          if (msg.type === 'result') {
            const strats = (msg.strategies as any[]) || []
            strategies = strats.map((s: any) => ({name: s.name, desc: s.doc || s.class, grade: ''}))
          }
        }).then(resolve).catch(reject)
      })
    } catch {
      strategies = [{name: 'trend_follow', desc: 'Bidirectional EMA-crossover trend follower (OSS demo strategy)', grade: 'C'}]
    }

    this.log('')
    this.log(`  ${bold('Select a strategy for algo trading:')}`)
    this.log('')
    for (let i = 0; i < strategies.length; i++) {
      const s = strategies[i]
      const gc = s.grade === 'A' ? green : cyan
      this.log(`    ${cyan(String(i + 1))}  ${bold(s.name.padEnd(22))} ${gc(s.grade)}  ${dim(s.desc)}`)
    }
    this.log(`    ${cyan(String(strategies.length + 1))}  ${dim('Enter custom name')}`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)
    const idx = parseInt(choice) - 1
    if (idx >= 0 && idx < strategies.length) return strategies[idx].name
    return await ask(`  ${cyan('Strategy name')}: `) || undefined
  }

  // ─── DAEMON LAUNCH ───

  private async startAlgo(
    strategy: string, pair: string, tf: string | undefined,
    equity: number, creds: NonNullable<ReturnType<typeof loadCredentials>>,
  ): Promise<void> {
    const key = sessionKey(strategy, pair)

    const engineArgs: string[] = [
      '--strategy', strategy,
      '--pair', pair,
      '--equity', String(equity),
      '--account', getAccountAddress(creds),
    ]
    if (tf) engineArgs.push('--tf', tf)

    // Spawn the engine as a background daemon
    const {pid} = spawnDaemon('algo', engineArgs, {
      HYPERLIQUID_PRIVATE_KEY: creds.private_key,
    })

    this.log(`  ${greenBg(' ● ALGO ')} Daemon started ${dim(`(PID ${pid})`)}`)
    this.log(`  ${dim('Trading runs in background. Press Ctrl+C to detach (trading continues).')}`)
    this.log('')

    // Wait briefly for the daemon to initialize and write first state
    await new Promise(r => setTimeout(r, 2000))

    // Attach the viewer
    await this.attachViewer(key)
  }

  // ─── FILE-BASED VIEWER ───

  private async attachViewer(key: string): Promise<void> {
    this.sessionStart = Date.now()
    this.dashboardActive = false
    this.ds = createDashboardState()
    this.ds.isLive = true
    this.viewerRunning = true

    // Ctrl+C detaches the viewer — does NOT stop the daemon
    const sigintHandler = () => {
      this.viewerRunning = false
    }
    process.on('SIGINT', sigintHandler)

    // Tick at 200ms — read state file and render
    this.tickTimer = setInterval(() => {
      if (!this.viewerRunning) return

      // Check if daemon is still alive
      if (!isSessionRunning(key)) {
        this.viewerRunning = false
        return
      }

      const snapshot = readSessionSnapshot(key)
      if (snapshot && snapshot.state) {
        const state = snapshot.state as Record<string, any>
        // Update dashboard state from snapshot
        this.ds.price = state.last_price ?? this.ds.price
        this.ds.priceDelta = state.price_delta ?? 0
        this.ds.sessionHigh = state.session_high ?? this.ds.sessionHigh
        this.ds.sessionLow = state.session_low ?? this.ds.sessionLow
        this.ds.equity = state.equity ?? this.ds.equity
        this.ds.totalEquity = state.total_equity ?? this.ds.totalEquity
        this.ds.totalPnlPct = state.total_pnl_pct ?? this.ds.totalPnlPct
        this.ds.unrealizedPnl = state.unrealized_pnl ?? this.ds.unrealizedPnl
        this.ds.numTrades = state.num_trades ?? this.ds.numTrades
        this.ds.winRate = state.win_rate ?? this.ds.winRate
        this.ds.totalFunding = state.total_funding ?? this.ds.totalFunding
        this.ds.fundingRate = state.last_funding_rate ?? this.ds.fundingRate
        this.ds.position = state.position ?? this.ds.position
        this.ds.stopProximity = state.stop_proximity ?? this.ds.stopProximity
        this.ds.priceHistory = state.price_history ?? this.ds.priceHistory
        this.ds.recentTrades = state.recent_trades ?? this.ds.recentTrades
        this.ds.strategy = state.strategy ?? this.ds.strategy
        this.ds.pair = state.pair ?? this.ds.pair
        this.ds.interval = state.interval ?? this.ds.interval
        this.ds.peakEquity = state.peak_equity ?? this.ds.peakEquity
        this.ds.wallet = state.wallet ?? this.ds.wallet

        // From snapshot envelope
        this.ds.fundingCountdownMin = snapshot.funding_countdown_min ?? this.ds.fundingCountdownMin
        this.ds.predictedFunding = snapshot.predicted_funding ?? this.ds.predictedFunding
        this.ds.candleRemainingBaseline = snapshot.candle_remaining_sec ?? this.ds.candleRemainingBaseline
        this.ds.candleProgressBaseline = snapshot.candle_progress ?? this.ds.candleProgressBaseline
        this.ds.heartbeatTime = Date.now()

        // Reasoning/indicators from snapshot
        const reasoning = snapshot.reasoning as Record<string, any> | undefined
        if (reasoning) {
          this.ds.reasoning = reasoning
          this.ds.conditions = reasoning.conditions ?? this.ds.conditions
          this.ds.signalStatus = reasoning.signal_status ?? this.ds.signalStatus
          this.ds.summary = reasoning.summary ?? this.ds.summary
        }
        if (snapshot.indicators) {
          this.ds.indicators = snapshot.indicators
        }

        this.renderAlgoDashboard()
      }
    }, 200)

    // Block until viewer exits
    await new Promise<void>(resolve => {
      const check = setInterval(() => {
        if (!this.viewerRunning) {
          clearInterval(check)
          resolve()
        }
      }, 100)
    })

    // Cleanup
    if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null }
    process.removeListener('SIGINT', sigintHandler)

    if (this.dashboardActive) this.clearDashboard()

    // Check if daemon is still running
    if (isSessionRunning(key)) {
      this.log('')
      this.log(`  ${dim('Dashboard detached. Trading continues in background.')}`)
      this.log(`  ${dim(`Run ${cyan('rift algo status')} to check or ${cyan('rift algo stop')} to end.`)}`)
      this.log('')
    } else {
      // Daemon exited — show final state
      const snapshot = readSessionSnapshot(key)
      if (snapshot?.state) {
        this.log('')
        this.renderSessionSummary(snapshot.state, snapshot as Record<string, any>)
        this.log('')
      } else {
        this.log('')
        this.log(`  ${dim('Session ended.')}`)
        this.log('')
      }
    }
  }

  // ─── STATUS ───

  private async showStatus(): Promise<void> {
    const pidsDir = getAlgoPidsDir()
    if (!fs.existsSync(pidsDir)) {
      this.log('')
      this.log(`  ${dim('No algo sessions running.')}`)
      this.log('')
      return
    }

    const pidFiles = fs.readdirSync(pidsDir).filter(f => f.endsWith('.pid'))
    if (pidFiles.length === 0) {
      this.log('')
      this.log(`  ${dim('No algo sessions running.')}`)
      this.log('')
      return
    }

    this.log('')
    this.log(`  ${bold('Algo Trading Sessions')}`)
    this.log(`  ${dim('─'.repeat(60))}`)

    let found = 0
    for (const pidFile of pidFiles) {
      const key = pidFile.replace('.pid', '')
      if (!isSessionRunning(key)) continue
      found++

      const snapshot = readSessionSnapshot(key)
      const state = snapshot?.state as Record<string, any> | undefined

      const strategy = state?.strategy ?? key.split('_')[0]
      const pair = state?.pair ?? key.split('_').slice(1).join('_')
      const equity = state?.total_equity ?? 0
      const pnlPct = state?.total_pnl_pct ?? 0
      const trades = state?.num_trades ?? 0
      const winRate = state?.win_rate ?? 0
      const position = state?.position
      const startedAt = state?.started_at ?? ''

      const pid = parseInt(fs.readFileSync(path.join(pidsDir, pidFile), 'utf-8').trim())

      const posStr = position
        ? `${position.side === 'long' ? green('LONG') : red('SHORT')} ${position.size?.toFixed(4)} @ $${position.entry_price}`
        : dim('FLAT')

      this.log('')
      this.log(`  ${greenBg(' ● ALGO ')} ${bold(strategy)} on ${pair} ${dim(`PID ${pid}`)}`)
      this.log(`    Equity: ${bold('$' + equity.toLocaleString())} ${colorPnl(pnlPct, '%')}  Trades: ${trades}  Win: ${winRate}%`)
      this.log(`    Position: ${posStr}`)
      if (startedAt) this.log(`    Started: ${dim(startedAt)}`)
    }

    if (found === 0) {
      this.log(`  ${dim('No algo sessions running.')}`)
    }

    this.log('')
  }

  // ─── STOP ───

  private async stopSession(pair: string, all: boolean): Promise<void> {
    const pidsDir = getAlgoPidsDir()
    if (!fs.existsSync(pidsDir)) {
      this.log(`\n  ${dim('No algo sessions running.')}\n`)
      return
    }

    const pidFiles = fs.readdirSync(pidsDir).filter(f => f.endsWith('.pid'))
    const running = pidFiles.filter(f => isSessionRunning(f.replace('.pid', '')))

    if (running.length === 0) {
      this.log(`\n  ${dim('No algo sessions running.')}\n`)
      return
    }

    if (all) {
      for (const pidFile of running) {
        const key = pidFile.replace('.pid', '')
        await this.stopByKey(key)
      }
      return
    }

    if (running.length === 1) {
      const key = running[0].replace('.pid', '')
      await this.stopByKey(key)
      return
    }

    // Multiple sessions — let user pick
    this.log('')
    this.log(`  ${bold('Select session to stop:')}`)
    this.log('')
    for (let i = 0; i < running.length; i++) {
      const key = running[i].replace('.pid', '')
      const snapshot = readSessionSnapshot(key)
      const strategy = snapshot?.state?.strategy ?? key
      const p = snapshot?.state?.pair ?? ''
      this.log(`    ${cyan(String(i + 1))}  ${bold(strategy)} on ${p}`)
    }
    this.log(`    ${cyan(String(running.length + 1))}  ${dim('Stop all')}`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)
    const idx = parseInt(choice) - 1

    if (idx === running.length) {
      // Stop all
      for (const pidFile of running) {
        await this.stopByKey(pidFile.replace('.pid', ''))
      }
    } else if (idx >= 0 && idx < running.length) {
      await this.stopByKey(running[idx].replace('.pid', ''))
    }
  }

  private async stopByKey(key: string): Promise<void> {
    const pidFile = path.join(getAlgoPidsDir(), `${key}.pid`)
    if (!fs.existsSync(pidFile)) return

    let pid: number
    try {
      pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
    } catch { return }

    this.log(`  ${dim('Stopping')} ${bold(key)} ${dim(`(PID ${pid})...`)}`)

    // Send SIGTERM for graceful shutdown
    try {
      process.kill(pid, 'SIGTERM')
    } catch {
      this.log(`  ${dim('Process already exited.')}`)
      try { fs.unlinkSync(pidFile) } catch {}
      return
    }

    // Wait for graceful shutdown (up to 30s)
    for (let i = 0; i < 60; i++) {
      try {
        process.kill(pid, 0) // test alive
        await new Promise(r => setTimeout(r, 500))
      } catch {
        break // process exited
      }
    }

    // Show final state
    const snapshot = readSessionSnapshot(key)
    if (snapshot?.state) {
      this.log('')
      this.renderSessionSummary(snapshot.state, snapshot as Record<string, any>)
    } else {
      this.log(`  ${green('✔')} Session stopped.`)
    }

    // Clean up PID file
    try { fs.unlinkSync(pidFile) } catch {}
    this.log('')
  }

  // ─── Dashboard (identical visual output) ───

  private renderAlgoDashboard(): void {
    const ds = this.ds
    const pos = ds.position
    const {remaining: candleRemaining, progress: candleProgress} = getLocalCountdown(ds)

    const elapsed = Math.floor((Date.now() - this.sessionStart) / 1000)
    const hours = Math.floor(elapsed / 3600)
    const mins = Math.floor((elapsed % 3600) / 60)
    const duration = hours > 0 ? `${hours}h ${mins}m` : `${mins}m`

    const lines: string[] = []

    // Header — LIVE badge + wallet + market type
    const walletShort = ds.wallet ? maskAddr(ds.wallet) : ''
    const mktTypes: Record<string, string> = {}
    const mkt = mktTypes[ds.strategy] || ''
    const mktTag = mkt ? ` ${dim('│')} ${mkt === 'All Conditions' ? cyan(mkt) : dim(mkt)}` : ''
    lines.push(`  ${greenBg(' ● ALGO ')} ${dim(walletShort)} ${dim('│')} ${dim(ds.strategy)}${mktTag} ${dim('│')} ${dim(duration)}`)

    // Price + chart
    const priceFixed = ds.price > 0 ? `$${ds.price.toFixed(2)}` : '$...'
    const priceCol = padEndVis(bold(priceFixed), 14)
    const deltaRaw = ds.priceDelta > 0 ? green(`▲ +$${ds.priceDelta.toFixed(0)}`)
      : ds.priceDelta < 0 ? red(`▼ -$${Math.abs(ds.priceDelta).toFixed(0)}`)
      : dim('─  $0')
    const deltaCol = padEndVis(deltaRaw, 10)
    const chart = sparkline(ds.priceHistory, 28)
    lines.push(`  ${priceCol} ${deltaCol} ${chart}`)

    // Session high/low
    if (ds.sessionHigh > 0 && ds.sessionLow > 0 && ds.sessionLow < ds.sessionHigh) {
      lines.push(`  ${dim('Session:')}  H ${dim('$' + ds.sessionHigh.toLocaleString())}  L ${dim('$' + ds.sessionLow.toLocaleString())}`)
    }

    // Strategy reasoning
    if (ds.conditions.length > 0) {
      lines.push('')
      lines.push(`  ${dim('STRATEGY')}`)
      for (const c of ds.conditions) {
        const pct = (c.pct as number) ?? 0
        const met = c.met as boolean
        const name = (c.name as string).padEnd(14)
        const detail = c.detail as string || ''
        const barWidth = 20
        const filled = Math.round(Math.min(1, pct) * barWidth)
        const empty = barWidth - filled
        let barColor: (s: string) => string
        if (met) barColor = green
        else if (pct >= 0.8) barColor = yellow
        else if (pct >= 0.5) barColor = cyan
        else barColor = dim
        const gauge = barColor('━'.repeat(filled)) + dim('╌'.repeat(empty))
        const icon = met ? green('✔') : dim('·')
        lines.push(`  ${icon} ${dim(name)} ${gauge} ${dim(String(c.value ?? ''))} ${dim(detail)}`)
      }
      const summaryColor = ds.signalStatus === 'entry_near' ? yellow
        : ds.signalStatus === 'warming' ? cyan
        : ds.signalStatus === 'in_position' ? green
        : ds.signalStatus === 'exit_near' ? red : dim
      lines.push(`  ${summaryColor('▸')} ${summaryColor(ds.summary)}`)
    }

    lines.push('')

    // Position or FLAT
    if (pos) {
      const sideColor = pos.side === 'long' ? green : red
      lines.push(`  ${sideColor('●')} ${sideColor(String(pos.side).toUpperCase())} ${(pos.size as number)?.toFixed(4)} @ $${(pos.entry_price as number)?.toLocaleString()} ${dim('│')} Unreal: ${colorPnl(ds.unrealizedPnl)} ${dim('│')} Hold: ${pos.candles_held}`)

      if (ds.stopProximity > 0.1) {
        const proxLabel = ds.stopProximity > 0.8 ? red('DANGER') : ds.stopProximity > 0.5 ? yellow('CAUTION') : dim('safe')
        lines.push(`  ${dim('SL')} ${proximityBar(ds.stopProximity)} ${proxLabel}`)
      }

      if (ds.fundingCountdownMin > 0) {
        lines.push(`  ${fundingCountdown(ds.fundingCountdownMin, ds.predictedFunding)}`)
      }
    } else {
      if (ds.recentCandles.length > 0) {
        lines.push(`  ${dim('RECENT CANDLES')}`)
        for (const c of ds.recentCandles.slice(-4)) {
          const changeColor = (c.change_pct as number) >= 0 ? green : red
          const arrow = (c.change_pct as number) >= 0 ? '▲' : '▼'
          lines.push(`  ${dim(String(c.time))}  ${dim('$')}${String(c.close).padEnd(10)} ${changeColor(arrow)} ${changeColor(String(c.change_pct) + '%')}`)
        }
      }
    }

    // Candle countdown
    if (candleRemaining > 0) {
      const countdownWidth = 30
      const filled = Math.round(candleProgress * countdownWidth)
      const empty = countdownWidth - filled
      const countdownBar = cyan('█'.repeat(filled)) + dim('░'.repeat(empty))
      const remainMin = Math.floor(candleRemaining / 60)
      const remainSec = candleRemaining % 60
      const remainStr = remainMin > 0 ? `${remainMin}:${String(remainSec).padStart(2, '0')}` : `${remainSec}s`
      lines.push(`  ${dim('NEXT CANDLE')}  ${countdownBar}  ${dim(remainStr)}`)
    }

    // Session stats
    lines.push('')
    lines.push(`  ${dim('EQUITY')} ${bold('$' + ds.totalEquity.toLocaleString())} ${colorPnl(ds.totalPnlPct, '%')}  ${dim('│')}  ${dim('TRADES')} ${ds.numTrades}  ${dim('WIN')} ${ds.winRate}%  ${dim('│')}  ${dim('FUNDING')} ${colorPnl(ds.totalFunding)}`)

    // Redraw with fixed height
    const H = Algo.DASHBOARD_HEIGHT

    while (lines.length < H) lines.push('')
    if (lines.length > H) lines.length = H

    if (this.dashboardActive) {
      process.stdout.write(`\x1b[${H}A`)
    }

    for (const line of lines) {
      process.stdout.write(`\x1b[2K${line}\n`)
    }

    this.dashboardActive = true
  }

  // ─── Session Summary ───

  private renderSessionSummary(state: Record<string, any>, msg: Record<string, any>): void {
    const narrative = msg.narrative as Record<string, any> | null
    const recentTrades = (state.recent_trades as Array<Record<string, any>>) || []
    const wallet = (state.wallet as string) || ''
    const iw = 58

    const row = (content: string) => {
      const pad = Math.max(0, iw - visLen(content))
      return `  ${bold('║')}${content}${' '.repeat(pad)}${bold('║')}`
    }
    const blank = row('')
    const divider = `  ${bold('╠' + '═'.repeat(iw) + '╣')}`

    this.log(`  ${bold('╔' + '═'.repeat(iw) + '╗')}`)
    this.log(`  ${bold('║')}  ${greenBg(' ● ALGO SESSION COMPLETE ')}${' '.repeat(iw - 28)}${bold('║')}`)
    this.log(divider)

    if (wallet) {
      this.log(row(`  Wallet: ${wallet}`))
    }

    const initialEquity = (state.initial_equity as number) ?? 0
    const totalEquity = (state.total_equity as number) ?? initialEquity
    const totalPnlPct = (state.total_pnl_pct as number) ?? 0
    const numTrades = (state.num_trades as number) ?? 0
    const winRate = (state.win_rate as number) ?? 0
    const totalFunding = (state.total_funding as number) ?? 0
    const peakEquity = (state.peak_equity as number) ?? initialEquity
    const candlesProcessed = (state.candles_processed as number) ?? 0

    if (narrative) {
      const story = narrative.story as string || ''
      const insight = narrative.insight as string || ''
      const projection = narrative.projection as Record<string, any> || {}

      if (story) {
        this.log(blank)
        this.log(row(`  ${bold('THE STORY:')}`))
        for (const line of this.wordWrap(story, iw - 4)) this.log(row(`  ${line}`))
      }
      if (insight) {
        this.log(blank)
        this.log(row(`  ${bold('KEY INSIGHT:')}`))
        for (const line of this.wordWrap(insight, iw - 4)) this.log(row(`  ${line}`))
      }

      this.log(blank)
      this.log(divider)

      const resultRow = (label: string, value: string) => {
        const lp = `  ${label}:`
        const gap = Math.max(1, iw - lp.length - visLen(value) - 1)
        return row(`${lp}${' '.repeat(gap)}${value}`)
      }

      this.log(resultRow('Starting Equity', `$${initialEquity.toLocaleString()}`))
      this.log(resultRow('Final Equity', `$${totalEquity.toLocaleString()}`))
      this.log(resultRow('Total P&L', colorPnl(totalPnlPct, '%')))
      this.log(resultRow('Trades', String(numTrades)))
      this.log(resultRow('Win Rate', `${winRate}%`))
      this.log(resultRow('Funding Collected', colorPnl(totalFunding)))
      this.log(resultRow('Peak Equity', `$${peakEquity.toLocaleString()}`))
      this.log(resultRow('Duration', `${candlesProcessed} candles`))

      if (projection && projection.daily) {
        this.log(blank)
        this.log(divider)
        this.log(row(`  ${bold('PROJECTION')} ${dim('(if session repeated)')}`))
        this.log(row(`  Daily: $${String(projection.daily).padEnd(10)} Monthly: $${projection.monthly}`))
        this.log(row(`  Annual: $${String(projection.annual).padEnd(9)} APY: ${projection.apy}%`))
        this.log(row(`  ${dim(`(based on ${projection.hours_observed}h — not a guarantee)`)}`))
      }
      this.log(blank)
    }

    if (recentTrades.length > 0) {
      this.log(divider)
      this.log(row(`  ${bold('TRADE LOG')}`))
      this.log(divider)
      for (const t of recentTrades) {
        const sideColor = t.side === 'long' ? green : red
        const pnlStr = colorPnl(t.pnl as number ?? 0)
        const reason = (t.exit_reason as string || '').replace(/_/g, ' ')
        const oid = t.oid ? dim(` oid:${t.oid}`) : ''
        this.log(row(`  ${sideColor('●')} ${sideColor(String(t.side).toUpperCase().padEnd(6))} ${padEndVis(pnlStr, 12)} ${dim(String(t.candles_held ?? 0) + 'c')} ${dim(reason)}${oid}`))
      }
      this.log(blank)
    }

    if (wallet) {
      this.log(divider)
      this.log(row(`  ${dim('Verified on Hyperliquid │ Wallet: ' + maskAddress(wallet))}`))
    }

    this.log(`  ${bold('╚' + '═'.repeat(iw) + '╝')}`)
    this.log('')
  }

  private wordWrap(text: string, maxWidth: number): string[] {
    const words = text.split(' ')
    const lines: string[] = []
    let current = ''
    for (const word of words) {
      if (current.length + word.length + 1 > maxWidth) { lines.push(current); current = word }
      else { current = current ? current + ' ' + word : word }
    }
    if (current) lines.push(current)
    return lines
  }
}
