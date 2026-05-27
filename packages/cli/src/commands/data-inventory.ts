import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class DataInventory extends GatedCommand {
  static override description =
    'Inventory of locally cached candles, funding, fills — counts + freshness'

  static override examples = [
    '$ rift data-inventory',
    '$ rift data-inventory --json',
  ]

  static override flags = {
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(DataInventory)
    await passthroughToEngine({
      command: 'data-inventory',
      args: [],
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      jsonOnly: flags.json,
    })
  }
}
