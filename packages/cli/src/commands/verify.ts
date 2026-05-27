import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class Verify extends GatedCommand {
  static override description =
    'Verify a strategy beats buy-and-hold over a date range — sanity check before going live'

  static override examples = [
    '$ rift verify trend_follow',
    '$ rift verify trend_follow --pair BTC --tf 4h --from 2024-01-01 --to 2024-12-31',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name', required: true}),
  }

  static override flags = {
    pair: Flags.string({description: 'Trading pair', default: 'BTC'}),
    tf: Flags.string({description: 'Timeframe', default: ''}),
    from: Flags.string({description: 'Start date YYYY-MM-DD', default: ''}),
    to: Flags.string({description: 'End date YYYY-MM-DD', default: ''}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Verify)
    const engineArgs = [args.strategy, '--pair', flags.pair]
    if (flags.tf) engineArgs.push('--tf', flags.tf)
    if (flags.from) engineArgs.push('--from', flags.from)
    if (flags.to) engineArgs.push('--to', flags.to)
    await passthroughToEngine({
      command: 'verify',
      args: engineArgs,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
