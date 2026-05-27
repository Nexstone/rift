import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class Guide extends GatedCommand {
  static override description =
    'Print the RIFT research-to-trade journey as a quick reference'

  static override examples = ['$ rift guide']

  async run(): Promise<void> {
    await passthroughToEngine({
      command: 'guide',
      args: [],
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
    })
  }
}
