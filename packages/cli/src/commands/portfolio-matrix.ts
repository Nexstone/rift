import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class PortfolioMatrix extends GatedCommand {
  static override description =
    'Strategy × pair P&L matrix, correlation matrix, and regime analysis'

  static override examples = [
    '$ rift portfolio-matrix',
    '$ rift portfolio-matrix --pairs BTC,ETH,SOL --strategies trend_follow,vol_breakout',
  ]

  static override flags = {
    pairs: Flags.string({description: 'Comma-separated coins', default: 'BTC,ETH,SOL'}),
    strategies: Flags.string({description: 'Comma-separated strategies (auto-discovers if empty)', default: ''}),
    equity: Flags.string({description: 'Starting equity per strategy', default: '10000'}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(PortfolioMatrix)
    const args = ['--pairs', flags.pairs, '--equity', flags.equity]
    if (flags.strategies) args.push('--strategies', flags.strategies)
    await passthroughToEngine({
      command: 'portfolio-matrix',
      args,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
