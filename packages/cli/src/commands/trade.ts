import {Args, Flags} from '@oclif/core'
import {createInterface} from 'node:readline'
import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import {loadCredentials, hasFullSetup, getAccountAddress} from '../lib/credentials.js'
import {BUILDER_FEE_DISPLAY} from '../lib/fees.js'
import type {EngineMessage} from '../lib/python-bridge.js'
import {
  green, red, yellow, cyan, bold, dim, greenBg,
  visLen, padEndVis, colorPnl,
  sparkline, proximityBar, fundingCountdown, maskAddress,
  createDashboardState, updateDashboardState, getLocalCountdown,
} from '../lib/tui.js'
import type {DashboardState} from '../lib/tui.js'

function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => { rl.close(); resolve(answer.trim()) })
  })
}

export default class Trade extends GatedCommand {
  static override description = 'Place a manual trade with stop loss and live monitoring'

  static override examples = [
    '$ rift trade ETH long --size 500',
    '$ rift trade SOL short --size 1000 --stop 3',
    '$ rift trade',
  ]

  static override args = {
    pair: Args.string({description: 'Coin (e.g. BTC, ETH, SOL)', required: false}),
    direction: Args.string({description: 'long or short', required: false}),
  }

  static override flags = {
    size: Flags.integer({description: 'Position size in USD', default: 0}),
    stop: Flags.string({description: 'Stop loss % (default: 2)', default: '2'}),
    leverage: Flags.integer({description: 'Leverage', default: 1}),
    // Internal: skip the "Type GO to execute" confirmation prompt. Used
    // by `rift perp long/short` which already require the user to type
    // the direction explicitly in the verb (e.g. `rift perp long BTC
    // --size 10`), so the second confirmation is redundant. The flag is
    // intentionally undocumented in the user-facing description — it
    // exists for internal delegation, not for users to type directly.
    yes: Flags.boolean({description: 'Skip the GO confirmation prompt (for internal delegation)', default: false, hidden: true}),
  }

  private dashboardActive = false
  private ds: DashboardState = createDashboardState()
  private tickTimer: ReturnType<typeof setInterval> | null = null
  private sessionStart = 0

  private static readonly DASHBOARD_HEIGHT = 14

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Trade)

    if (!hasFullSetup()) {
      this.log(`\n  ${red('✘')} Trade requires wallet setup. Run: ${cyan('rift auth setup')}\n`)
      return
    }

    const creds = loadCredentials()
    if (!creds) {
      this.log(`\n  ${red('✘')} No credentials.\n`)
      return
    }

    // Interactive mode if no args
    let pair = args.pair || ''
    let direction = args.direction || ''
    let sizeUsd = flags.size || 0
    const stopPct = parseFloat(flags.stop!) / 100

    if (!pair) {
      this.log('')
      this.log(`  ${bold('Quick Trade')}`)
      this.log(`  ${dim('─'.repeat(40))}`)
      this.log('')
      pair = (await ask(`  ${cyan('Coin')} ${dim('(BTC)')}: `)) || 'BTC'
    }

    if (!direction) {
      direction = await ask(`  ${cyan('Direction')} ${dim('(long/short)')}: `)
      if (!direction || !['long', 'short'].includes(direction.toLowerCase())) {
        this.log(`  ${dim('Cancelled.')}`)
        return
      }
    }

    if (!sizeUsd) {
      const sizeStr = await ask(`  ${cyan('Size in USD')} ${dim('(500)')}: `)
      sizeUsd = parseInt(sizeStr) || 500
    }

    pair = pair.toUpperCase()
    direction = direction.toLowerCase()
    const dirColor = direction === 'long' ? green : red

    // Confirmation
    this.log('')
    this.log(`  ${bold('╔════════════════════════════════════════╗')}`)
    this.log(`  ${bold('║')}  ${dirColor(direction.toUpperCase())} ${bold(pair)}  $${sizeUsd.toLocaleString()}${' '.repeat(Math.max(1, 22 - pair.length - String(sizeUsd).length))}${bold('║')}`)
    this.log(`  ${bold('║')}  Stop: ${(stopPct * 100).toFixed(1)}%   Fee: ${BUILDER_FEE_DISPLAY}${' '.repeat(17)}${bold('║')}`)
    this.log(`  ${bold('║')}  Leverage: ${flags.leverage}x${' '.repeat(27)}${bold('║')}`)
    this.log(`  ${bold('╚════════════════════════════════════════╝')}`)
    this.log('')

    if (!flags.yes) {
      const confirm = await ask(`  ${cyan('Type "GO" to execute')}: `)
      if (confirm !== 'GO') {
        this.log(dim('\n  Cancelled.\n'))
        return
      }
    }

    this.log('')
    await this.executeTrade(pair, direction, sizeUsd, stopPct, flags.leverage!, creds)
  }

  private async executeTrade(
    pair: string, direction: string, sizeUsd: number,
    stopPct: number, leverage: number,
    creds: NonNullable<ReturnType<typeof loadCredentials>>,
  ): Promise<void> {
    this.sessionStart = Date.now()
    this.dashboardActive = false
    this.ds = createDashboardState()
    this.ds.isLive = true

    process.env.HYPERLIQUID_PRIVATE_KEY = creds.private_key

    const engineArgs = [
      pair, direction,
      '--size', String(sizeUsd),
      '--stop', String(stopPct),
      '--leverage', String(leverage),
      '--account', getAccountAddress(creds),
    ]

    // Dashboard tick
    this.tickTimer = setInterval(() => {
      if (this.dashboardActive) this.renderDashboard()
    }, 200)

    // Ctrl+C closes position (not detach — this is manual trading)
    const {getEngineProcess} = await import('../lib/python-bridge.js')
    let enginePromise: Promise<void> | null = null

    const sigintHandler = () => {
      if (enginePromise) {
        const proc = getEngineProcess(enginePromise)
        if (proc && !proc.killed) proc.kill('SIGTERM')
      }
    }
    process.on('SIGINT', sigintHandler)

    try {
      enginePromise = runEngine('manual-trade', engineArgs, (msg: EngineMessage) => {
        const state = msg.state as Record<string, any> | undefined

        if (msg.type === 'status') {
          if (!this.dashboardActive) {
            this.log(`  ${dim(String(msg.msg))}`)
          }
        } else if (msg.type === 'trade') {
          this.handleTradeEvent(msg)
        } else if (msg.type === 'heartbeat') {
          if (state) {
            updateDashboardState(this.ds, state, msg as Record<string, any>, 'heartbeat')
            this.renderDashboard()
          }
        } else if (msg.type === 'shutdown') {
          if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null }
          if (this.dashboardActive) this.clearDashboard()

          if (msg.trade_replay) {
            this.renderTradeReplay(msg.trade_replay as Record<string, any>)
          }

          // Shareable card
          const card = msg.shareable_card as string
          if (card) {
            this.log('')
            this.log(dim('  Shareable:'))
            this.log('')
            for (const line of card.split('\n')) {
              this.log(`  ${line}`)
            }
          }
          this.log('')
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })

      await enginePromise
    } catch (error: any) {
      const errMsg = String(error?.message ?? '')
      if (!errMsg.includes('SIGTERM') && !errMsg.includes('SIGINT') && !errMsg.includes('null')) {
        this.log(`  ${red('✘')} ${errMsg.split('\n')[0] || 'Unknown error'}`)
      }
    } finally {
      if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null }
      process.removeListener('SIGINT', sigintHandler)
      delete process.env.HYPERLIQUID_PRIVATE_KEY
    }
  }

  private handleTradeEvent(msg: EngineMessage): void {
    const action = msg.action as string
    if (this.dashboardActive) this.clearDashboard()

    if (action === 'open') {
      const side = (msg.side as string) ?? ''
      const price = (msg.price as number) ?? 0
      const size = (msg.size as number) ?? 0
      const sl = (msg.stop_loss as number) ?? 0
      const sideColor = side === 'long' ? green : red

      this.log(`  ${greenBg(' TRADE ')} ${sideColor('▶')} ${bold(side.toUpperCase())} ${size.toFixed(4)} @ $${price.toLocaleString()}`)
      this.log(`    ${dim('Stop:')} $${sl.toLocaleString()} ${dim('(on Hyperliquid)')}`)
      this.log(`    ${dim('Press Ctrl+C to close position')}`)
      this.log('')
    } else if (action === 'stop_loss') {
      this.log(`  ${red('✘')} Stop loss triggered ${dim('(Hyperliquid server-side)')}`)
    }
  }

  private renderTradeReplay(replay: Record<string, any>): void {
    const isWin = replay.result === 'WIN'
    const resultColor = isWin ? green : red
    const side = (replay.side as string).toUpperCase()

    this.log('')
    this.log(`  ${greenBg(' RESULT ')} ${resultColor(side)} — ${resultColor(replay.result as string)}`)
    this.log(`    Entry: $${replay.entry_price}  Exit: $${replay.exit_price}`)
    this.log(`    ${bold('P&L:')} ${colorPnl(replay.total_pnl as number)} (${colorPnl(replay.pnl_pct as number, '%')})`)
    if (replay.funding_pnl) this.log(`    Funding: ${colorPnl(replay.funding_pnl as number)}`)
    this.log(`    Duration: ${replay.duration}`)
  }

  private clearDashboard(): void {
    if (this.dashboardActive) {
      const h = Trade.DASHBOARD_HEIGHT
      process.stdout.write(`\x1b[${h}A`)
      for (let i = 0; i < h; i++) process.stdout.write('\x1b[2K\n')
      process.stdout.write(`\x1b[${h}A`)
      this.dashboardActive = false
    }
  }

  private renderDashboard(): void {
    const ds = this.ds
    const pos = ds.position
    const elapsed = Math.floor((Date.now() - this.sessionStart) / 1000)
    const mins = Math.floor(elapsed / 60)
    const secs = elapsed % 60
    const duration = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`

    const lines: string[] = []

    // Header
    lines.push(`  ${greenBg(' TRADE ')} ${dim(ds.pair || '')} ${dim('│')} ${dim(duration)} ${dim('│ Ctrl+C to close')}`)

    // Price
    const priceStr = ds.price > 0 ? `$${ds.price.toFixed(2)}` : '$...'
    const deltaRaw = ds.priceDelta > 0 ? green(`+$${ds.priceDelta.toFixed(0)}`)
      : ds.priceDelta < 0 ? red(`-$${Math.abs(ds.priceDelta).toFixed(0)}`)
      : dim('$0')
    const chart = sparkline(ds.priceHistory, 28)
    lines.push(`  ${bold(priceStr)}  ${deltaRaw}  ${chart}`)

    lines.push('')

    // Position
    if (pos) {
      const sideColor = pos.side === 'long' ? green : red
      lines.push(`  ${sideColor('●')} ${sideColor(String(pos.side).toUpperCase())} ${(pos.size as number)?.toFixed(4)} @ $${(pos.entry_price as number)?.toLocaleString()}`)
      lines.push(`  ${dim('Unrealized:')} ${colorPnl(ds.unrealizedPnl)}  ${dim('Funding:')} ${colorPnl(ds.totalFunding)}`)

      if (ds.stopProximity > 0.1) {
        const proxLabel = ds.stopProximity > 0.8 ? red('DANGER') : ds.stopProximity > 0.5 ? yellow('CAUTION') : dim('safe')
        lines.push(`  ${dim('SL')} ${proximityBar(ds.stopProximity)} ${proxLabel}`)
      }

      if (ds.fundingCountdownMin > 0) {
        lines.push(`  ${fundingCountdown(ds.fundingCountdownMin, ds.predictedFunding)}`)
      }
    }

    lines.push('')
    lines.push(`  ${dim('EQUITY')} ${bold('$' + ds.totalEquity.toLocaleString())} ${colorPnl(ds.totalPnlPct, '%')}`)

    // Fixed height
    const H = Trade.DASHBOARD_HEIGHT
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
}
