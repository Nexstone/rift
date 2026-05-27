import {GatedCommand} from '../../lib/base-command.js'
import {createInterface} from 'node:readline'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {getDataDir} from '../../lib/python-bridge.js'
import {
  green, red, yellow, cyan, bold, dim,
} from '../../lib/tui.js'

function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => { rl.close(); resolve(answer.trim()) })
  })
}

interface StrategyEntry {
  name: string
  pair: string
  max_allocation: number
  schedule: string | {start: string; stop: string}
  enabled: boolean
}

export default class PortfolioCreate extends GatedCommand {
  static override description = 'Interactive wizard to build a portfolio configuration'

  static override examples = [
    '$ rift portfolio create',
  ]

  // Strategies discovered dynamically — users create their own. Default
  // shows only OSS-shipped strategies; user's workbench customs are added
  // at runtime by the picker.
  private readonly availableStrategies = [
    {name: 'trend_follow', grade: 'C', desc: 'Bidirectional EMA-crossover trend follower (OSS demo strategy)'},
  ]

  async run(): Promise<void> {
    this.log('')
    this.log(`  ${bold('Portfolio Builder')}`)
    this.log(`  ${dim('─'.repeat(50))}`)
    this.log('')

    const portfolioName = await ask(`  ${cyan('Portfolio name')} ${dim('(e.g. nexstone_alpha)')}: `) || 'my_portfolio'

    const strategies: StrategyEntry[] = []
    let totalAlloc = 0

    // Add strategies
    let addMore = true
    while (addMore) {
      this.log('')
      this.log(`  ${bold('Add a strategy')} ${dim(`(${(100 - totalAlloc * 100).toFixed(0)}% remaining)`)}`)
      this.log('')

      for (let i = 0; i < this.availableStrategies.length; i++) {
        const s = this.availableStrategies[i]
        const gc = s.grade === 'A' ? green : cyan
        this.log(`    ${cyan(String(i + 1))}  ${s.name.padEnd(22)} ${gc(s.grade)}  ${dim(s.desc)}`)
      }
      this.log(`    ${cyan(String(this.availableStrategies.length + 1))}  ${dim('Enter custom name')}`)
      this.log('')

      const choice = await ask(`  ${cyan('>')} `)
      const idx = parseInt(choice) - 1
      let stratName: string
      if (idx >= 0 && idx < this.availableStrategies.length) {
        stratName = this.availableStrategies[idx].name
      } else {
        stratName = await ask(`  ${cyan('Strategy name')}: `)
        if (!stratName) continue
      }

      const pair = (await ask(`  ${cyan('Pair')} ${dim('(BTC)')}: `)) || 'BTC'

      const remainPct = Math.round((1 - totalAlloc) * 100)
      const allocStr = await ask(`  ${cyan('Max allocation %')} ${dim(`(${Math.min(40, remainPct)}%)`)}: `)
      const allocPct = parseInt(allocStr) || Math.min(40, remainPct)
      const alloc = allocPct / 100

      const schedStr = await ask(`  ${cyan('Schedule')} ${dim('(always / HH:MM-HH:MM UTC)')}: `) || 'always'
      let schedule: string | {start: string; stop: string} = 'always'
      if (schedStr !== 'always' && schedStr.includes('-')) {
        const [start, stop] = schedStr.split('-').map(s => s.trim())
        schedule = {start: start + ' UTC', stop: stop + ' UTC'}
      }

      strategies.push({
        name: stratName,
        pair: pair.toUpperCase(),
        max_allocation: alloc,
        schedule,
        enabled: true,
      })
      totalAlloc += alloc

      this.log(`  ${green('✔')} Added ${bold(stratName)} on ${pair} (${allocPct}%${schedStr !== 'always' ? ', ' + schedStr : ''})`)

      if (totalAlloc >= 1.0) {
        this.log(`  ${dim('100% allocated.')}`)
        addMore = false
      } else {
        const more = await ask(`  ${cyan('Add another?')} ${dim('(yes/no)')}: `)
        addMore = more.toLowerCase() === 'yes' || more.toLowerCase() === 'y'
      }
    }

    if (strategies.length === 0) {
      this.log(`\n  ${dim('No strategies added. Cancelled.')}\n`)
      return
    }

    // Risk limits
    this.log('')
    this.log(`  ${bold('Risk Limits')}`)
    this.log('')

    const maxDDStr = await ask(`  ${cyan('Max portfolio drawdown %')} ${dim('(15%)')}: `)
    const maxDD = (parseInt(maxDDStr) || 15) / 100

    const maxNetStr = await ask(`  ${cyan('Max net exposure %')} ${dim('(100%)')}: `)
    const maxNet = (parseInt(maxNetStr) || 100) / 100

    // Alerts
    this.log('')
    this.log(`  ${bold('Alerts')}`)
    this.log('')
    const webhookUrl = await ask(`  ${cyan('Slack webhook URL')} ${dim('(blank to skip)')}: `)

    // Build YAML
    const alertConfigs: any[] = [{type: 'log', events: ['all']}]
    if (webhookUrl) {
      alertConfigs.push({
        type: 'webhook',
        url: webhookUrl,
        events: ['trade', 'stop_loss', 'health_drop', 'health_rotation', 'drawdown_warning', 'drawdown_kill', 'session_died'],
      })
    }

    const config: Record<string, any> = {
      name: portfolioName,
      strategies: strategies.map(s => ({
        name: s.name,
        pair: s.pair,
        enabled: s.enabled,
        schedule: s.schedule,
        max_allocation: s.max_allocation,
      })),
      risk: {
        max_net_exposure: maxNet,
        max_gross_exposure: 1.5,
        max_per_asset: 0.8,
        max_drawdown: maxDD,
      },
      rotation: {
        enabled: true,
        pause_grade: 'D',
        stop_grade: 'F',
        check_interval: 5,
      },
      alerts: alertConfigs,
    }

    // Write YAML
    const yamlLines: string[] = []
    yamlLines.push(`# Portfolio: ${portfolioName}`)
    yamlLines.push(`name: ${portfolioName}`)
    yamlLines.push('')
    yamlLines.push('strategies:')
    for (const s of config.strategies) {
      yamlLines.push(`  - name: ${s.name}`)
      yamlLines.push(`    pair: ${s.pair}`)
      yamlLines.push(`    enabled: ${s.enabled}`)
      if (typeof s.schedule === 'object') {
        yamlLines.push(`    schedule:`)
        yamlLines.push(`      start: "${s.schedule.start}"`)
        yamlLines.push(`      stop: "${s.schedule.stop}"`)
      } else {
        yamlLines.push(`    schedule: ${s.schedule}`)
      }
      yamlLines.push(`    max_allocation: ${s.max_allocation}`)
    }
    yamlLines.push('')
    yamlLines.push('risk:')
    yamlLines.push(`  max_net_exposure: ${config.risk.max_net_exposure}`)
    yamlLines.push(`  max_gross_exposure: ${config.risk.max_gross_exposure}`)
    yamlLines.push(`  max_per_asset: ${config.risk.max_per_asset}`)
    yamlLines.push(`  max_drawdown: ${config.risk.max_drawdown}`)
    yamlLines.push('')
    yamlLines.push('rotation:')
    yamlLines.push(`  enabled: ${config.rotation.enabled}`)
    yamlLines.push(`  pause_grade: ${config.rotation.pause_grade}`)
    yamlLines.push(`  stop_grade: ${config.rotation.stop_grade}`)
    yamlLines.push(`  check_interval: ${config.rotation.check_interval}`)
    yamlLines.push('')
    yamlLines.push('alerts:')
    for (const a of config.alerts) {
      yamlLines.push(`  - type: ${a.type}`)
      if (a.url) yamlLines.push(`    url: ${a.url}`)
      yamlLines.push(`    events: [${a.events.join(', ')}]`)
    }

    const configDir = path.join(getDataDir(), 'algo')
    fs.mkdirSync(configDir, {recursive: true})
    const configPath = path.join(configDir, 'portfolio.yaml')
    fs.writeFileSync(configPath, yamlLines.join('\n') + '\n')

    this.log('')
    this.log(`  ${green('✔')} Portfolio config saved: ${dim(configPath)}`)
    this.log('')
    this.log(`  ${bold('Summary')}`)
    for (const s of strategies) {
      const schedDesc = typeof s.schedule === 'object'
        ? `${s.schedule.start.replace(' UTC', '')}-${s.schedule.stop.replace(' UTC', '')} UTC`
        : s.schedule
      this.log(`    ${s.name.padEnd(20)} ${s.pair.padEnd(5)} ${(s.max_allocation * 100).toFixed(0)}%  ${dim(schedDesc)}`)
    }
    this.log(`    Max drawdown: ${(maxDD * 100).toFixed(0)}%  Net limit: ${(maxNet * 100).toFixed(0)}%`)
    if (webhookUrl) this.log(`    Alerts: Slack webhook configured`)
    this.log('')
    this.log(`  ${dim(`Run ${cyan('rift portfolio start')} to begin trading.`)}`)
    this.log('')
  }
}
