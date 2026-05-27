import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class Lessons extends GatedCommand {
  static override description = 'Show captured trading lessons (post-trade learnings)'

  static override examples = [
    '$ rift lessons',
    '$ rift lessons --strategy trend_follow',
  ]

  static override flags = {
    coin: Flags.string({description: 'Filter by coin', default: ''}),
    strategy: Flags.string({description: 'Filter by strategy name', default: ''}),
    limit: Flags.string({description: 'Number of lessons to show', default: '20'}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(Lessons)
    const args: string[] = []
    if (flags.coin) args.push('--coin', flags.coin)
    if (flags.strategy) args.push('--strategy', flags.strategy)
    args.push('--limit', flags.limit)
    await passthroughToEngine({
      command: 'lessons',
      args,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      jsonOnly: flags.json,
    })
  }
}
