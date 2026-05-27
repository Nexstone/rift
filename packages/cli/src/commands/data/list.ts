import {GatedCommand} from '../../lib/base-command.js'
import {runEngine} from '../../lib/python-bridge.js'
import type {EngineMessage} from '../../lib/python-bridge.js'

export default class DataList extends GatedCommand {
  static override description = 'List cached candle data'

  static override examples = [
    '$ rift data list',
  ]

  async run(): Promise<void> {
    await runEngine('list-data', [], (msg: EngineMessage) => {
      if (msg.type === 'result') {
        const data = msg.data as Array<{pair: string; interval: string; rows: number; start: number; end: number}>
        if (!data || data.length === 0) {
          this.log('No cached data. Run: rift data fetch --pair BTC-PERP')
          return
        }

        this.log('')
        this.log('  Pair          Interval    Candles     Start                  End')
        this.log('  ──────────────────────────────────────────────────────────────────')
        for (const d of data) {
          const start = new Date(d.start).toISOString().slice(0, 16)
          const end = new Date(d.end).toISOString().slice(0, 16)
          this.log(`  ${d.pair.padEnd(14)} ${d.interval.padEnd(12)} ${String(d.rows).padEnd(12)} ${start}  ${end}`)
        }

        this.log('')
      }
    })
  }
}
