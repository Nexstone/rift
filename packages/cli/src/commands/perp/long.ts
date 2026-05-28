import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'

export default class PerpLong extends GatedCommand {
  static override description =
    'Open a LONG position on Hyperliquid perps with a stop loss and live monitor.'

  static override examples = [
    '$ rift perp long BTC --size 50 --stop 2     # $50 long, 2% stop',
    '$ rift perp long ETH --size 25 --leverage 3 # $25 long at 3x',
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
    const {args, flags} = await this.parse(PerpLong)
    // Delegate to `rift trade` which already has the rich live monitor
    // dashboard for the manual-trade lifecycle. This wrapper exists so
    // `rift perp long BTC` reads naturally without the user having to
    // type "long" twice or remember the `rift trade <pair> <direction>`
    // positional-arg order.
    // --yes skips the "Type GO" prompt that `rift trade` shows for the
    // legacy `rift trade BTC long` form — `rift perp long BTC` already
    // requires the user to type the direction in the verb, so a second
    // confirmation is redundant. The position-size + stop are still on
    // the command line and the manual-trade daemon's risk disclaimers
    // remain in place.
    await this.config.runCommand('trade', [
      args.coin, 'long',
      '--size', String(flags.size),
      '--stop', flags.stop,
      '--leverage', String(flags.leverage),
      '--yes',
    ])
  }
}
