import {Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'
import {passthroughToEngine} from '../../lib/engine-passthrough.js'

export default class PerpStatus extends GatedCommand {
  static override description =
    'Show current perp account state — positions, margin used, unrealized PnL, open orders.'

  static override examples = [
    '$ rift perp status',
    '$ rift perp status --json',
  ]

  static override flags = {
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(PerpStatus)
    // `state` is the canonical engine command for "what's my account
    // doing right now": positions, equity, margin, algo sessions, etc.
    await passthroughToEngine({
      command: 'state',
      args: [],
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
