import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'

export default class PerpShort extends GatedCommand {
  static override description =
    'Open a SHORT position on Hyperliquid perps with a stop loss and live monitor.'

  static override examples = [
    '$ rift perp short BTC --size 50 --stop 2     # $50 short, 2% stop',
    '$ rift perp short ETH --size 25 --leverage 3 # $25 short at 3x',
  ]

  static override args = {
    coin: Args.string({description: 'Coin (e.g. BTC, ETH, SOL)', required: true}),
  }

  static override flags = {
    size: Flags.integer({description: 'Position size in USD', required: true}),
    stop: Flags.string({description: 'Stop loss % (default: 2)', default: '2'}),
    leverage: Flags.integer({description: 'Leverage multiplier', default: 1}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(PerpShort)
    await this.config.runCommand('trade', [
      args.coin, 'short',
      '--size', String(flags.size),
      '--stop', flags.stop,
      '--leverage', String(flags.leverage),
      '--yes',
    ])
  }
}
