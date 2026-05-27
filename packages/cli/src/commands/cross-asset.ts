import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class CrossAsset extends GatedCommand {
  static override description =
    'Cross-asset correlation matrix + lead-lag + beta-vs-benchmark'

  static override examples = [
    '$ rift cross-asset',
    '$ rift cross-asset --coins BTC,ETH,SOL --tf 4h --benchmark BTC',
  ]

  static override flags = {
    coins: Flags.string({
      description: 'Comma-separated coin list',
      default: 'BTC,ETH,SOL,SUI,AVAX,NEAR,LINK,DOGE',
    }),
    tf: Flags.string({description: 'Timeframe', default: '1h'}),
    lookback: Flags.string({description: 'Candles to use (720 = 30d at 1h)', default: '720'}),
    benchmark: Flags.string({description: 'Beta-vs-benchmark coin', default: 'BTC'}),
    'max-lag': Flags.string({description: 'Lead-lag search window (candles)', default: '6'}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(CrossAsset)
    const args = [
      '--coins', flags.coins,
      '--tf', flags.tf,
      '--lookback', flags.lookback,
      '--benchmark', flags.benchmark,
      '--max-lag', flags['max-lag'],
    ]
    await passthroughToEngine({
      command: 'cross-asset',
      args,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      jsonOnly: flags.json,
    })
  }
}
