import {Args, Flags} from '@oclif/core'
import * as path from 'node:path'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class PortfolioBacktest extends GatedCommand {
  static override description = 'Run a multi-strategy portfolio backtest from a portfolio.yaml'

  static override examples = [
    '$ rift portfolio-backtest portfolio.yaml',
    '$ rift portfolio-backtest config.yaml --strategies-dir ./strategies',
  ]

  static override args = {
    config: Args.string({description: 'Path to portfolio.yaml', required: true}),
  }

  static override flags = {
    'strategies-dir': Flags.string({description: 'Directory with strategy .py files', default: ''}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(PortfolioBacktest)
    // The engine subprocess is spawned with cwd=engine/, so relative paths
    // in args would resolve from there instead of the user's terminal.
    // Anchor to the user's cwd before handing the path to Python.
    const configPath = path.resolve(args.config)
    const engineArgs = [configPath]
    if (flags['strategies-dir']) {
      engineArgs.push('--strategies-dir', path.resolve(flags['strategies-dir']))
    }
    await passthroughToEngine({
      command: 'portfolio-backtest',
      args: engineArgs,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
