import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'
import {
  green, red, yellow, cyan, bold, dim,
  bar, colorPnl,
} from '../lib/tui.js'

const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

export default class Scout extends GatedCommand {
  static override description = 'Scan the market and find trading opportunities ranked by confluence'

  static override examples = [
    '$ rift scout',
    '$ rift scout --top 10',
    '$ rift scout --no-soak           # skip 120s websocket soak (faster, less accurate)',
    '$ rift scout --soak 30           # shorter soak window',
  ]

  static override flags = {
    top: Flags.integer({description: 'Number of coins to scan', default: 20}),
    tf: Flags.string({description: 'Timeframe (bias)', default: '1h'}),
    min: Flags.integer({description: 'Minimum confluence score', default: 2}),
    soak: Flags.integer({
      description: 'Seconds to collect live websocket data (default 120, lower = faster)',
      default: 120,
    }),
    'no-soak': Flags.boolean({
      description: 'Skip the websocket soak phase entirely (fastest, less accurate)',
      default: false,
    }),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(Scout)

    this.log('')
    this.log(`  ${bold('RIFT Scout')} ${dim('— scanning top ' + flags.top + ' coins...')}`)
    this.log('')

    // ─── Startup spinner ─────────────────────────────────────
    //
    // Python boot + HL HTTP roundtrips take 3-5s before the engine emits
    // its first message. Run a spinner so the user knows something's
    // happening. Killed by the first message of any type we render below.

    let firstMessageSeen = false
    let spinnerFrame = 0
    let spinnerLine = ''
    const spinnerTimer = setInterval(() => {
      if (firstMessageSeen) return
      if (spinnerLine) process.stdout.write('\x1b[1A\x1b[2K')
      const frame = SPINNER_FRAMES[spinnerFrame % SPINNER_FRAMES.length]
      spinnerLine = `  ${cyan(frame)} ${dim('Connecting to Hyperliquid + loading market context...')}`
      process.stdout.write(spinnerLine + '\n')
      spinnerFrame++
    }, 80)

    const stopSpinner = (): void => {
      clearInterval(spinnerTimer)
      if (spinnerLine && !firstMessageSeen) {
        // First-time stop: erase the spinner line so the next render starts clean
        process.stdout.write('\x1b[1A\x1b[2K')
      }
      firstMessageSeen = true
      spinnerLine = ''
    }

    // ─── Live render state ───────────────────────────────────

    let activeLine = ''  // any single-line indicator currently on screen

    const clearActiveLine = (): void => {
      if (activeLine) {
        process.stdout.write('\x1b[1A\x1b[2K')
        activeLine = ''
      }
    }

    const renderProgressBar = (pct: number, label: string): string => {
      const filled = Math.round(pct / 100 * 30)
      const empty = 30 - filled
      return `  ${cyan('█'.repeat(filled))}${dim('░'.repeat(empty))}  ${String(pct).padStart(3)}%  ${dim(label)}`
    }

    // ─── Build engine args ───────────────────────────────────

    const engineArgs: string[] = [
      '--top', String(flags.top),
      '--bias-tf', flags.tf!,
      '--min', String(flags.min),
    ]
    if (flags['no-soak']) {
      engineArgs.push('--no-soak')
    } else {
      engineArgs.push('--soak', String(flags.soak))
    }

    // ─── Run engine ──────────────────────────────────────────

    try {
      await runEngine('scout', engineArgs, (msg: EngineMessage) => {
        stopSpinner()

        if (msg.type === 'status') {
          clearActiveLine()
          this.log(`  ${dim(String(msg.msg))}`)
          return
        }

        if (msg.type === 'soak') {
          // Engine emits {elapsed, total, trades} every 10s during the soak.
          const elapsed = (msg.elapsed as number) ?? 0
          const total = (msg.total as number) ?? 1
          const trades = (msg.trades as number) ?? 0
          const pct = Math.min(100, Math.round((elapsed / total) * 100))
          clearActiveLine()
          activeLine = renderProgressBar(
            pct,
            `soak ${elapsed}/${total}s · ${trades.toLocaleString()} trades`,
          )
          process.stdout.write(activeLine + '\n')
          return
        }

        if (msg.type === 'progress') {
          const pct = (msg.pct as number) ?? 0
          const coin = (msg.coin as string) ?? ''
          const phase = (msg.phase as string) ?? ''
          const label = phase ? `${phase}: ${coin}` : coin
          clearActiveLine()
          activeLine = renderProgressBar(pct, label)
          process.stdout.write(activeLine + '\n')
          return
        }

        if (msg.type === 'error') {
          clearActiveLine()
          this.log(`  ${red('✘')} ${msg.msg}`)
          return
        }

        if (msg.type === 'result') {
          clearActiveLine()
          this.renderResults(msg, flags.min)
        }
      })
    } catch (error: any) {
      stopSpinner()
      clearActiveLine()
      this.log(`  ${red('✘')} ${error.message}`)
    }
  }

  private renderResults(msg: EngineMessage, minConfluence: number): void {
    const opportunities = (msg.opportunities as Array<Record<string, any>>) || []

    if (opportunities.length === 0) {
      this.log(`  ${dim('No opportunities with ' + minConfluence + '+ confluence found.')}`)
      this.log(`  ${dim('Try: rift scout --min 1')}`)
      this.log('')
      return
    }

    this.log(`  ${bold('TOP OPPORTUNITIES')}`)
    this.log(`  ${dim('─'.repeat(55))}`)
    this.log('')

    for (let i = 0; i < Math.min(opportunities.length, 10); i++) {
      const o = opportunities[i]
      const dir = o.direction as string
      const dirColor = dir === 'LONG' ? green : red
      const conf = o.confluence as number
      const score = o.score as number
      const confBar = bar(conf, 5, 10)

      this.log(`  ${bold('#' + (i + 1))}  ${bold(String(o.coin).padEnd(6))} ${dirColor(dir.padEnd(6))} ${confBar}  ${conf}/5 confluence   Score: ${bold(String(Math.round(score)))}`)

      // Show signals
      const signals = (o.signals as Array<Record<string, any>>) || []
      for (const s of signals) {
        this.log(`      ${dim(String(s.detail))}`)
      }

      // Entry/Stop/Target
      this.log('')
      const entry = o.entry_price as number
      const stop = o.stop_price as number
      const target = o.target_price as number
      const rr = o.risk_reward as number
      const vol = o.volume_24h as number

      this.log(`      Entry: $${entry?.toLocaleString()}   Stop: $${stop?.toLocaleString()}   Target: $${target?.toLocaleString()}`)
      this.log(`      R/R: ${bold('1:' + (rr || 0).toFixed(1))}   24h Vol: $${_formatVol(vol)}`)
      this.log('')
    }

    this.log(`  ${dim('─'.repeat(55))}`)
    this.log('')
    this.log(`  ${dim('Execute:')}`)

    for (let i = 0; i < Math.min(3, opportunities.length); i++) {
      const o = opportunities[i]
      this.log(`    ${cyan(`rift trade ${o.coin} ${(o.direction as string).toLowerCase()} --size 500`)}`)
    }

    this.log('')
  }
}

function _formatVol(vol: number): string {
  if (vol >= 1_000_000_000) return (vol / 1_000_000_000).toFixed(1) + 'B'
  if (vol >= 1_000_000) return (vol / 1_000_000).toFixed(0) + 'M'
  if (vol >= 1_000) return (vol / 1_000).toFixed(0) + 'K'
  return String(Math.round(vol))
}
