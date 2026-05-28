import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../../lib/base-command.js'
import {passthroughToEngine} from '../../lib/engine-passthrough.js'
import {loadCredentials, hasFullSetup, getAccountAddress} from '../../lib/credentials.js'

export default class PerpClose extends GatedCommand {
  static override description =
    'Close an open perp position (and cancel any orders for the coin) via reduce-only IOC market order.'

  static override examples = [
    '$ rift perp close BTC      # close BTC perp position + cancel BTC orders',
    '$ rift perp close          # close ALL perp positions + cancel ALL orders',
  ]

  static override args = {
    coin: Args.string({
      description: 'Coin to close (omit to close all open positions)',
      required: false,
    }),
  }

  static override flags = {
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(PerpClose)
    if (!hasFullSetup()) {
      this.error('Perp close requires wallet setup. Run: rift auth setup')
    }
    const creds = loadCredentials()
    if (!creds) {
      this.error('No credentials. Run: rift auth setup')
    }

    // The engine's `close-all` command reads HYPERLIQUID_PRIVATE_KEY
    // from env (not CLI args, for security — see comment in close-all
    // source). The spawned engine subprocess inherits process.env, so
    // we set it here before passthroughToEngine spawns.
    process.env.HYPERLIQUID_PRIVATE_KEY = creds.private_key
    try {
      // Delegates to the engine's `close-all` command, which is the
      // direct close path (reduce-only IOC market order). Distinct from
      // `close-position` which writes a command file for a running algo
      // daemon — that's the right call when you have an algo session
      // managing a position; `perp close` is for the manual-trade lifecycle
      // or for emergency cleanup.
      const engineArgs: string[] = ['--account', getAccountAddress(creds)]
      if (args.coin) engineArgs.push('--coin', args.coin)
      await passthroughToEngine({
        command: 'close-all',
        args: engineArgs,
        log: (m) => this.log(m),
        error: (m) => this.error(m),
        exit: (c) => this.exit(c),
        jsonOnly: flags.json,
      })
    } finally {
      delete process.env.HYPERLIQUID_PRIVATE_KEY
    }
  }
}
