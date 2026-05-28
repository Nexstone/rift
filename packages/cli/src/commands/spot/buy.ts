import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'
import {passthroughToEngine} from '../../lib/engine-passthrough.js'

export default class SpotBuy extends GatedCommand {
  static override description =
    'Buy a token on the Hyperliquid spot market. Simple purchase, no leverage. 1% builder fee on sell side only.'

  static override examples = [
    '$ rift spot buy HYPE --amount 25       # spend 25 USDC buying HYPE',
    '$ rift spot buy BTC --amount 50        # 50 USDC of BTC (auto-resolved to UBTC spot)',
    '$ rift spot buy UETH --size 0.01       # buy a fixed 0.01 UETH',
  ]

  static override args = {
    coin: Args.string({
      description: 'Token to buy (e.g. HYPE, BTC, ETH — names auto-resolve to HL spot tokens like UBTC, UETH)',
      required: true,
    }),
  }

  static override flags = {
    amount: Flags.string({description: 'USDC amount to spend (one of --amount or --size required)', default: ''}),
    size: Flags.string({description: 'Token amount to buy (alternative to --amount)', default: ''}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(SpotBuy)
    const engineArgs: string[] = [args.coin]
    if (flags.amount) engineArgs.push('--amount', flags.amount)
    if (flags.size) engineArgs.push('--size', flags.size)
    await passthroughToEngine({
      command: 'buy',
      args: engineArgs,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
