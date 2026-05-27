import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {passthroughToEngine} from '../lib/engine-passthrough.js'

export default class PairsBacktest extends GatedCommand {
  static override description = 'Backtest a pairs/spread trade (e.g. BTC/ETH spread)'

  static override examples = [
    '$ rift pairs-backtest',
    '$ rift pairs-backtest --a BTC --b ETH --tf 1h --entry-z 2.0',
  ]

  static override flags = {
    a: Flags.string({description: 'First asset', default: 'BTC'}),
    b: Flags.string({description: 'Second asset', default: 'ETH'}),
    tf: Flags.string({description: 'Candle interval', default: '1h'}),
    equity: Flags.string({description: 'Starting equity USD', default: '10000'}),
    lookback: Flags.string({description: 'Rolling window for z-score (hours)', default: '168'}),
    'entry-z': Flags.string({description: 'Z-score entry threshold', default: '2.0'}),
    'exit-z': Flags.string({description: 'Z-score exit threshold', default: '0.5'}),
    'stop-z': Flags.string({description: 'Z-score stop loss', default: '4.0'}),
    'max-hold': Flags.string({description: 'Max hold time in candles', default: '72'}),
    json: Flags.boolean({description: 'Emit raw JSON only', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(PairsBacktest)
    const args = [
      '--a', flags.a, '--b', flags.b, '--tf', flags.tf,
      '--equity', flags.equity, '--lookback', flags.lookback,
      '--entry-z', flags['entry-z'], '--exit-z', flags['exit-z'],
      '--stop-z', flags['stop-z'], '--max-hold', flags['max-hold'],
    ]
    await passthroughToEngine({
      command: 'pairs-backtest',
      args,
      log: (m) => this.log(m),
      error: (m) => this.error(m),
      exit: (c) => this.exit(c),
      jsonOnly: flags.json,
    })
  }
}
