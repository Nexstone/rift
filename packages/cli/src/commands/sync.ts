import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class Sync extends GatedCommand {
  static override description =
    'Sync historical OHLCV + funding data from the Hyperliquid S3 archive. Requires AWS credentials (free tier; ~$2 for the full download).'

  static override examples = [
    '$ rift sync --coins BTC --tf 1h,4h',
    '$ rift sync --coins BTC,ETH,SOL --tf 5m,15m,1h,4h --start 2024-01-01',
    '$ rift sync --aws-key AKIA... --aws-secret ... --coins BTC --tf 1h',
  ]

  static override flags = {
    coins: Flags.string({
      description: 'Comma-separated coins (empty = auto-detect from registered strategies)',
      default: '',
    }),
    tf: Flags.string({
      description: 'Comma-separated timeframes to build',
      default: '5m,15m,1h,4h',
    }),
    start: Flags.string({description: 'Start date YYYY-MM-DD', default: '2023-09-01'}),
    end: Flags.string({description: 'End date YYYY-MM-DD (default: today)', default: ''}),
    'no-funding': Flags.boolean({description: 'Skip funding rate sync', default: false}),
    full: Flags.boolean({description: 'Full sync (ignore incremental cache)', default: false}),
    'aws-key': Flags.string({description: 'AWS Access Key ID (for non-interactive setup)', default: ''}),
    'aws-secret': Flags.string({description: 'AWS Secret Access Key', default: ''}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(Sync)
    const args: string[] = []
    if (flags.coins) args.push('--coins', flags.coins)
    args.push('--tf', flags.tf)
    args.push('--start', flags.start)
    if (flags.end) args.push('--end', flags.end)
    if (flags['no-funding']) args.push('--no-funding')
    if (flags.full) args.push('--full')
    if (flags['aws-key']) args.push('--aws-key', flags['aws-key'])
    if (flags['aws-secret']) args.push('--aws-secret', flags['aws-secret'])

    await passthroughToEngine({
      command: 'sync',
      args,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
