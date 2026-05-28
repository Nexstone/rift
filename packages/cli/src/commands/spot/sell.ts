import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'
import {passthroughToEngine} from '../../lib/engine-passthrough.js'

export default class SpotSell extends GatedCommand {
  static override description =
    'Sell a token from spot holdings back to USDC. 1% builder fee applies.'

  static override examples = [
    '$ rift spot sell HYPE              # sell all HYPE holdings',
    '$ rift spot sell HYPE --pct 50     # sell half',
    '$ rift spot sell HYPE --amount 5   # sell a fixed 5 HYPE',
    '$ rift spot sell BTC               # sell all UBTC (BTC alias)',
  ]

  static override args = {
    coin: Args.string({
      description: 'Token to sell (e.g. HYPE, BTC, ETH — names auto-resolve to HL spot tokens)',
      required: true,
    }),
  }

  static override flags = {
    amount: Flags.string({description: 'Token amount to sell (0/omit = all)', default: ''}),
    pct: Flags.string({description: 'Percentage to sell (e.g. 50 = half)', default: ''}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(SpotSell)
    const engineArgs: string[] = [args.coin]
    if (flags.amount) engineArgs.push('--amount', flags.amount)
    if (flags.pct) engineArgs.push('--pct', flags.pct)
    await passthroughToEngine({
      command: 'sell',
      args: engineArgs,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
