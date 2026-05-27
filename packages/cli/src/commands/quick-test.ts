import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class QuickTest extends GatedCommand {
  static override description =
    'Fast backtest with delta comparison to the last run — the core iteration loop while tuning a strategy.'

  static override examples = [
    '$ rift quick-test trend_follow --pair BTC',
    '$ rift quick-test trend_follow --pair ETH --tf 4h --change "tightened stop to 1.5%"',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name', required: true}),
  }

  static override flags = {
    pair: Flags.string({description: 'Trading pair', default: 'BTC'}),
    tf: Flags.string({description: 'Timeframe (auto from config if empty)', default: ''}),
    equity: Flags.string({description: 'Starting equity', default: '10000'}),
    leverage: Flags.string({description: 'Leverage multiplier', default: '1'}),
    change: Flags.string({
      description: 'Description of what changed (recorded in the delta history)',
      default: '',
    }),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(QuickTest)
    const engineArgs: string[] = [
      args.strategy,
      '--pair', flags.pair,
      '--equity', flags.equity,
      '--leverage', flags.leverage,
    ]
    if (flags.tf) engineArgs.push('--tf', flags.tf)
    if (flags.change) engineArgs.push('--change', flags.change)

    await passthroughToEngine({
      command: 'quick-test',
      args: engineArgs,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
