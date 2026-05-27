import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'

const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const yellow = (s: string) => `\x1b[33m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

export default class Cost extends GatedCommand {
  static override description =
    'Estimate pre-trade cost for a hypothetical trade: fees + funding + impact + slippage'

  static override examples = [
    '$ rift cost BTC 50000',
    '$ rift cost ETH 10000 --side sell --hold 24',
    '$ rift cost BTC 100000 --maker --tier-vol-14d 5000000',
  ]

  static override args = {
    pair: Args.string({description: 'Trading pair (e.g. BTC, ETH-PERP)', required: true}),
    notional: Args.string({
      description: 'Trade size in USD notional (e.g. 50000)',
      required: true,
    }),
  }

  static override flags = {
    side: Flags.string({description: 'buy / sell / long / short', default: 'buy'}),
    tf: Flags.string({description: 'Candle interval for ADV / vol calc', default: '1h'}),
    hold: Flags.string({description: 'Holding period in hours (for funding accrual)', default: '0'}),
    maker: Flags.boolean({description: 'Treat as maker (post-only)', default: false}),
    spot: Flags.boolean({description: 'Treat as spot trade instead of perp', default: false}),
    'no-builder-fee': Flags.boolean({description: 'Exclude RIFT builder fee', default: false}),
    'tier-vol-14d': Flags.string({
      description: 'Your 14d HL volume USD (fee-tier lookup)',
      default: '0',
    }),
    json: Flags.boolean({description: 'Emit raw JSON instead of human format', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Cost)

    const engineArgs: string[] = [args.pair, args.notional]
    engineArgs.push('--side', flags.side)
    engineArgs.push('--tf', flags.tf)
    engineArgs.push('--hold', flags.hold)
    if (flags.maker) engineArgs.push('--maker')
    if (flags.spot) engineArgs.push('--spot')
    if (flags['no-builder-fee']) engineArgs.push('--no-builder-fee')
    engineArgs.push('--tier-vol-14d', flags['tier-vol-14d'])

    let result: Record<string, unknown> | null = null

    await runEngine('cost', engineArgs, (msg: EngineMessage) => {
      if (msg.type === 'result') {
        result = msg as Record<string, unknown>
      } else if (msg.type === 'error') {
        this.error(msg.msg as string)
      }
    })

    if (!result) {
      this.error('No result returned from engine')
    }

    if (flags.json) {
      this.log(JSON.stringify(result, null, 2))
      return
    }

    // Pretty render
    const r = result as any
    const c = r.cost
    const pair = r.pair as string
    const side = r.side as string
    const notional = r.notional_usd as number
    const mid = r.mid_price as number
    const advPct = r.adv_pct as number | null

    this.log('')
    this.log(`  ${bold('Pre-trade cost estimate')}  ${dim(`${pair} ${side}`)}`)
    this.log(`  ${dim('─'.repeat(56))}`)
    this.log(`  Notional       $${notional.toLocaleString()}`)
    if (mid > 0) {
      this.log(`  Mid price      $${mid.toLocaleString()}`)
    }
    if (advPct !== null && advPct !== undefined) {
      this.log(`  % of ADV       ${advPct.toFixed(3)}%`)
    }
    this.log('')
    this.log(`  Component        bps      USD`)
    this.log(`  ${dim('─'.repeat(40))}`)
    this.log(`  Fees           ${(c.fee_bps as number).toFixed(2).padStart(6)}   $${(c.fee_usd as number).toFixed(2).padStart(8)}`)
    if (c.funding_bps !== 0) {
      this.log(`  Funding        ${(c.funding_bps as number).toFixed(2).padStart(6)}   $${(c.funding_usd as number).toFixed(2).padStart(8)}`)
    }
    this.log(`  Impact         ${(c.impact_bps as number).toFixed(2).padStart(6)}   $${(c.impact_usd as number).toFixed(2).padStart(8)}   ${dim('[' + (c.impact_model || '?') + ']')}`)
    if (c.slippage_bps !== 0) {
      this.log(`  Slippage       ${(c.slippage_bps as number).toFixed(2).padStart(6)}   $${(c.slippage_usd as number).toFixed(2).padStart(8)}`)
    }
    this.log(`  ${dim('─'.repeat(40))}`)
    this.log(`  ${bold('TOTAL')}          ${(c.total_bps as number).toFixed(2).padStart(6)}   $${(c.total_usd as number).toFixed(2).padStart(8)}`)
    this.log('')

    const warnings = (r.warnings as string[]) || []
    if (warnings.length > 0) {
      for (const w of warnings) {
        this.log(`  ${yellow('!')} ${w}`)
      }
      this.log('')
    }
  }
}
