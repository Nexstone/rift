import {GatedCommand} from '../../lib/base-command.js'
import {runEngine} from '../../lib/python-bridge.js'
import type {EngineMessage} from '../../lib/python-bridge.js'

interface StrategyInfo {
  name: string
  class: string
  config: Record<string, string>
  doc: string
}

export default class StrategiesList extends GatedCommand {
  static override description = 'List available trading strategies'

  static override examples = [
    '$ rift strategies list',
  ]

  async run(): Promise<void> {
    await runEngine('strategies', [], (msg: EngineMessage) => {
      if (msg.type === 'result') {
        const strategies = msg.strategies as StrategyInfo[]

        if (!strategies || strategies.length === 0) {
          this.log('No strategies found. Create one with: rift new <name>')
          return
        }

        this.log('')
        this.log('  Available strategies:')
        this.log('')

        for (const s of strategies) {
          this.log(`  ${s.name}`)
          if (s.doc) this.log(`    ${s.doc.trim()}`)
          if (Object.keys(s.config).length > 0) {
            this.log('    Config:')
            for (const [key, val] of Object.entries(s.config)) {
              this.log(`      ${key}: ${val}`)
            }
          }

          this.log('')
        }
      }
    })
  }
}
