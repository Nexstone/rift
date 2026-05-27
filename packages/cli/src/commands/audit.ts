import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class Audit extends GatedCommand {
  static override description = 'Export compliance-grade audit trail of all live trades'

  static override examples = [
    '$ rift audit',
    '$ rift audit --export json --last 90',
    '$ rift audit --strategy trend_follow --output ./audit.csv',
  ]

  static override flags = {
    export: Flags.string({description: 'Export format: csv or json', default: 'csv'}),
    last: Flags.string({description: 'Days of history to include', default: '30'}),
    strategy: Flags.string({description: 'Filter by strategy name', default: ''}),
    output: Flags.string({description: 'Custom output path', default: ''}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(Audit)
    const args = ['--export', flags.export, '--last', flags.last]
    if (flags.strategy) args.push('--strategy', flags.strategy)
    if (flags.output) args.push('--output', flags.output)
    await passthroughToEngine({
      command: 'audit',
      args,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      jsonOnly: flags.json,
    })
  }
}
