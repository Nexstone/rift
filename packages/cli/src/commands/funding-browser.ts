import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class FundingBrowser extends GatedCommand {
  static override description =
    'Browse funding rates across coins — current + window stats + extremes'

  static override examples = [
    '$ rift funding-browser',
    '$ rift funding-browser --top 50',
    '$ rift funding-browser --coins BTC,ETH,SOL --days 30',
  ]

  static override flags = {
    coins: Flags.string({description: 'Comma-separated coin list (default: all cached)', default: ''}),
    top: Flags.string({description: 'Number of coins to show, ranked by current funding', default: '20'}),
    days: Flags.string({description: 'History window in days', default: '7'}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(FundingBrowser)
    const args: string[] = []
    if (flags.coins) args.push('--coins', flags.coins)
    args.push('--top', flags.top)
    args.push('--days', flags.days)
    await passthroughToEngine({
      command: 'funding-browser',
      args,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      jsonOnly: flags.json,
    })
  }
}
