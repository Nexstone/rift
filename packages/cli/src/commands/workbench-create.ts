import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class WorkbenchCreate extends GatedCommand {
  static override description =
    'Create a new custom strategy from a workbench template (config-as-data — no Python required).'

  static override examples = [
    '$ rift workbench-create my_strategy',
    '$ rift workbench-create rsi_revert --template single_signal_example',
  ]

  static override args = {
    // Engine validator requires letters/numbers/underscores only — reject
    // hyphens up front so the user sees a clear error from oclif's parser
    // rather than the engine's "Invalid strategy name" reply.
    name: Args.string({
      description: 'Name for the new strategy (letters, numbers, underscores)',
      required: true,
    }),
  }

  static override flags = {
    template: Flags.string({
      description: 'Template to seed from',
      options: ['blank', 'single_signal_example'],
      default: 'blank',
    }),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(WorkbenchCreate)
    const engineArgs: string[] = [args.name, '--template', flags.template]

    await passthroughToEngine({
      command: 'workbench-create',
      args: engineArgs,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
