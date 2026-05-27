import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'
import {
  green, red, yellow, cyan, bold, dim,
  colorNum, colorPnl, gradeColor, bar,
  boxRow, boxTop, boxBottom, boxDivider,
  boldBoxRow, boldBoxTop, boldBoxBottom, boldBoxDivider,
  resultRow, visLen, padEndVis, ask,
} from '../lib/tui.js'

// Market type color coding
const marketColor = (m: string) => {
  if (m === 'All Conditions') return cyan(m)
  if (m === 'Adaptive') return cyan(m)
  if (m.startsWith('Sideways')) return dim(m)
  if (m.includes('Trend')) return yellow(m)
  if (m === 'Mean-Reverting') return yellow(m)
  if (m === 'Breakout') return yellow(m)
  return dim(m)
}

// Strategy catalog with descriptions
// Strategies discovered dynamically from engine — users create their own
const STRATEGIES: {name: string; desc: string; stats: string; ret: number; sharpe: number; grade: string; validated: boolean; tf: string; market: string}[] = []

const TEMPLATES = [
  {key: '1', label: 'Funding template', desc: 'funding rate capture with EMA trend filter', template: 'funding'},
  {key: '2', label: 'VWAP reversion', desc: 'VWAP deviation mean reversion', template: 'vwap_reversion'},
  {key: '3', label: 'Trend following', desc: 'EMA crossover with ADX filter', template: 'trend_follow'},
  {key: '4', label: 'Blank', desc: 'empty strategy, build from scratch', template: 'blank'},
]

export default class Research extends GatedCommand {
  static override description = 'Research Lab — discover, test, build, optimize, and compare strategies'

  static override examples = [
    '$ rift research',
    '$ rift research my_strategy --pair SUI',
  ]

  static override args = {
    strategy: Args.string({description: 'Strategy name (interactive if omitted)', required: false}),
  }

  static override flags = {
    pair: Flags.string({description: 'Ticker symbol (e.g. BTC, ETH, SOL)', default: 'BTC'}),
    tf: Flags.string({description: 'Timeframe (auto-detected if omitted)'}),
    equity: Flags.integer({description: 'Starting equity', default: 10000}),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(Research)

    if (args.strategy) {
      return this.runPipeline(args.strategy, flags.pair!, flags.tf, flags.equity!)
    }

    return this.mainMenu()
  }

  // ═══════════════════════════════════════════
  //  MAIN MENU
  // ═══════════════════════════════════════════

  private async mainMenu(): Promise<void> {
    // Gather live stats for the dashboard header
    let customCount = 0
    let testCount = 0
    try {
      await runEngine('workbench-list', [], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          customCount = ((msg.strategies as Array<any>) || []).length
        }
      })
    } catch { /* empty */ }

    // Find best grade and return from validated strategies (if any).
    // STRATEGIES is currently empty by design (users discover strategies
    // dynamically); guard against an empty-array reduce TypeError.
    const bestStrategy = STRATEGIES.length > 0
      ? STRATEGIES.reduce((a, b) => a.ret > b.ret ? a : b)
      : null

    const iw = 51  // inner width

    this.log('')
    this.log(boldBoxTop(iw))
    this.log(boldBoxRow(iw)(`  ${bold('RESEARCH LAB')}`))
    this.log(boldBoxDivider(iw))

    // Stat boxes — 3 columns
    const col = 16  // column width
    const bestVal = bestStrategy
      ? `${gradeColor(bestStrategy.grade)} ${green('+' + bestStrategy.ret + '%')}`
      : dim('—')
    const valCount = bold(String(STRATEGIES.length))
    const custCount = bold(String(customCount))

    // Labels
    this.log(boldBoxRow(iw)(`  ${padEndVis(dim('BEST'), col)}${padEndVis(dim('VALIDATED'), col)}${dim('CUSTOM')}`))
    // Values
    this.log(boldBoxRow(iw)(`  ${padEndVis(bestVal, col)}${padEndVis(valCount, col)}${custCount}`))

    this.log(boldBoxDivider(iw))

    // Menu options — use boldBoxRow for proper alignment
    const brow = boldBoxRow(iw)
    this.log(brow(''))
    this.log(brow(`  ${cyan('1')}  Test         ${dim('validate a strategy')}`))
    this.log(brow(`  ${cyan('2')}  Explore      ${dim('browse what works')}`))
    this.log(brow(`  ${cyan('3')}  Build        ${dim('create in the workbench')}`))
    this.log(brow(`  ${cyan('4')}  Optimize     ${dim('find best parameters')}`))
    this.log(brow(`  ${cyan('5')}  Compare      ${dim('head-to-head showdown')}`))
    this.log(brow(''))
    this.log(brow(`  ${cyan('0')}  ${dim('Exit')}`))
    this.log(brow(''))
    this.log(boldBoxBottom(iw))
    this.log('')
    this.log(`  ${dim('Tip:')} ${cyan('rift more')} ${dim('shows every engine command (102 total)')}`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    switch (choice) {
      case '0':
      case 'q':
      case 'b':
      case 'B':
        return
      case '1': return this.testMenu()
      case '2': return this.exploreMenu()
      case '3': return this.buildMenu()
      case '4': return this.optimizeMenu()
      case '5': return this.compareMenu()
      default:
        if (choice) this.log(dim('  Invalid selection.'))
        return this.mainMenu()
    }
  }

  // ═══════════════════════════════════════════
  //  SHARED STRATEGY PICKER
  // ═══════════════════════════════════════════

  /**
   * Show a numbered list of all strategies (validated + custom).
   * Returns the selected strategy name, or null if cancelled.
   * If multi=true, allows comma-separated selection and returns names joined by comma.
   */
  private async pickStrategy(prompt: string = 'Select a strategy', multi: boolean = false): Promise<string | null> {
    // Fetch custom workbench strategies
    let customStrategies: Array<Record<string, any>> = []
    try {
      await runEngine('workbench-list', [], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          customStrategies = ((msg.strategies as Array<Record<string, any>>) || [])
        }
      })
    } catch { /* empty */ }

    this.log('')
    this.log(`  ${bold(prompt + ':')}`)
    this.log('')

    // Validated strategies
    const allStrategies: Array<{name: string; desc: string; tag: string; market: string}> = []

    for (const s of STRATEGIES) {
      allStrategies.push({
        name: s.name,
        desc: s.desc,
        tag: `${gradeColor(s.grade)} ${padEndVis(colorNum(s.ret, '%'), 10)}`,
        market: s.market,
      })
    }

    // Custom strategies
    for (const s of customStrategies) {
      allStrategies.push({
        name: String(s.name),
        desc: String(s.description || '').slice(0, 40),
        tag: dim(`v${s.version} custom`),
        market: '',
      })
    }

    // Display
    for (let i = 0; i < allStrategies.length; i++) {
      const s = allStrategies[i]
      const mkt = s.market ? `${padEndVis(marketColor(s.market), 24)} ` : ''
      this.log(`    ${cyan(String(i + 1))}  ${bold(s.name.padEnd(22))} ${s.tag} ${mkt} ${dim(s.desc)}`)
    }
    this.log(`    ${cyan(String(allStrategies.length + 1))}  ${dim('Enter custom name')}`)
    this.log('')

    if (multi) {
      this.log(dim('  Enter numbers separated by commas (e.g., 1,2,4)'))
    }

    const choice = await ask(`  ${cyan('>')} `)

    if (multi) {
      // Parse comma-separated numbers
      const parts = choice.split(',').map(s => s.trim())
      const names: string[] = []
      for (const part of parts) {
        const idx = parseInt(part) - 1
        if (idx >= 0 && idx < allStrategies.length) {
          names.push(allStrategies[idx].name)
        }
      }
      if (names.length > 0) return names.join(',')
      // Fallback to raw input (they may have typed names)
      return choice || null
    }

    const idx = parseInt(choice) - 1
    if (idx >= 0 && idx < allStrategies.length) {
      return allStrategies[idx].name
    }
    if (parseInt(choice) === allStrategies.length + 1) {
      return await ask(`  ${cyan('Strategy name')}: `) || null
    }
    // If they typed a name directly, use it
    if (choice && isNaN(parseInt(choice))) return choice
    return null
  }

  // ═══════════════════════════════════════════
  //  1. TEST — Full validation pipeline
  // ═══════════════════════════════════════════

  private async testMenu(): Promise<void> {
    const strategy = await this.pickStrategy('Test a strategy')
    if (!strategy) return this.mainMenu()

    const pair = (await ask(`  ${cyan('Ticker')} ${dim('(BTC)')}: `) || 'BTC').replace('-PERP', '').replace('-perp', '').toUpperCase()

    this.log('')
    await this.runPipeline(strategy, pair)
    return this.mainMenu()
  }

  // ═══════════════════════════════════════════
  //  2. EXPLORE — Discovery hub
  // ═══════════════════════════════════════════

  private async exploreMenu(): Promise<void> {
    const iw = 60
    const brow = boldBoxRow(iw)
    this.log('')
    this.log(boldBoxTop(iw))
    this.log(brow(`  ${bold('EXPLORE')} ${dim('— discover what exists before building')}`))
    this.log(boldBoxDivider(iw))
    this.log(brow(''))
    this.log(brow(`  ${cyan('1')}  Indicator catalog       ${dim('50+ indicators, filterable')}`))
    this.log(brow(`  ${cyan('2')}  Strategy showcase       ${dim('validated + custom')}`))
    this.log(brow(`  ${cyan('3')}  Market scanner          ${dim('rift scout — live opportunities')}`))
    this.log(brow(`  ${cyan('4')}  Signal forensics        ${dim('stats / decay / backfill')}`))
    this.log(brow(`  ${cyan('5')}  Funding rate browser    ${dim('current + 7d + extremes')}`))
    this.log(brow(`  ${cyan('6')}  Order flow browser      ${dim('taker ratio / imbalance / flow')}`))
    this.log(brow(`  ${cyan('7')}  Cross-asset matrix      ${dim('correlation / lead-lag / beta')}`))
    this.log(brow(`  ${cyan('8')}  Regime browser          ${dim('vol + trend regime now & history')}`))
    this.log(brow(''))
    this.log(brow(`  ${cyan('b')}  Back to Research Lab`))
    this.log(boldBoxBottom(iw))
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)
    switch (choice) {
      case '1': return this.indicatorCatalogMenu()
      case '2': return this.strategyShowcaseMenu()
      case '3': await this.config.runCommand('scout'); return this.exploreMenu()
      case '4': return this.signalForensicsMenu()
      case '5': return this.fundingBrowserMenu()
      case '6': return this.orderFlowBrowserMenu()
      case '7': return this.crossAssetMenu()
      case '8': return this.regimeBrowserMenu()
      case 'b': case 'B': return this.mainMenu()
      default:
        this.log(dim('  Invalid selection.'))
        return this.exploreMenu()
    }
  }

  // ─── Strategy showcase (formerly exploreMenu) ─────────────

  private async strategyShowcaseMenu(): Promise<void> {
    const iw = 56  // inner width for strategy cards
    const row = boxRow(iw)

    // Dynamic discovery — pull whatever's actually registered (shipped OSS
    // + private + workbench customs). No hardcoded list of RIFT-team-only
    // strategies that an OSS user wouldn't have on disk.
    let registered: Array<{name: string; doc?: string; class?: string}> = []
    try {
      await runEngine('strategies', [], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          registered = ((msg.strategies as Array<Record<string, any>>) || []).map(s => ({
            name: String(s.name),
            doc: s.doc ? String(s.doc) : undefined,
            class: s.class ? String(s.class) : undefined,
          }))
        }
      })
    } catch { /* empty registry handled below */ }

    let customs: Array<Record<string, any>> = []
    try {
      await runEngine('workbench-list', [], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          customs = (msg.strategies as Array<Record<string, any>>) || []
        }
      })
    } catch { /* empty */ }

    this.log('')
    this.log(boldBoxTop(iw + 2))
    this.log(`  ${bold('║')} ${'Strategy Explorer'.padStart(Math.floor((iw) / 2) + 9).padEnd(iw + 1)}${bold('║')}`)
    this.log(boldBoxBottom(iw + 2))
    this.log('')

    if (registered.length === 0 && customs.length === 0) {
      this.log(`  ${yellow('!')} No strategies registered yet.`)
      this.log('')
      this.log(`  ${dim('Get started:')}`)
      this.log(`    ${cyan('rift new my-strategy')}                  ${dim('— scaffold from template')}`)
      this.log(`    ${cyan('rift backtest trend_follow --pair BTC --tf 4h')}  ${dim('— try the shipped example')}`)
      this.log('')
    } else {
      if (registered.length > 0) {
        this.log(dim('  Registered (shipped + custom):'))
        this.log('')
        for (const s of registered) {
          const doc = (s.doc || s.class || '').toString().split('\n')[0].slice(0, iw - 4)
          this.log(boxTop(iw))
          this.log(row(`${green('★')} ${bold(s.name)}`))
          if (doc) this.log(row(`  ${dim(doc)}`))
          this.log(boxBottom(iw))
        }
        this.log('')
      }
      if (customs.length > 0) {
        this.log(dim('  Your workbench strategies:'))
        this.log('')
        for (const s of customs) {
          this.log(`    ${cyan(String(s.name).padEnd(22))} v${s.version}  ${dim(String(s.description || '').slice(0, 40))}`)
        }
        this.log('')
      }
    }

    this.log(`  ${bold('What next?')}`)
    this.log(`    ${cyan('1')}  Test a strategy on a specific pair`)
    this.log(`    ${cyan('2')}  See which pairs work best for a strategy`)
    this.log(`    ${cyan('3')}  Back to Explore`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    switch (choice) {
      case '1': return this.testMenu()
      case '2': {
        const strat = await this.pickStrategy('Which strategy to test across pairs')
        if (!strat) return this.exploreMenu()
        this.log('')
        await this.config.runCommand('backtest', [strat, '--all-pairs', '--top', '10'])
        return this.exploreMenu()
      }
      case '3': return this.exploreMenu()
      default: return this.exploreMenu()
    }
  }

  // ═══════════════════════════════════════════
  //  3. BUILD — Strategy Workbench
  // ═══════════════════════════════════════════

  private async buildMenu(): Promise<void> {
    this.log('')
    this.log(`  ${bold('╔═══════════════════════════════════════════╗')}`)
    this.log(`  ${bold('║          Strategy Workbench               ║')}`)
    this.log(`  ${bold('╚═══════════════════════════════════════════╝')}`)
    this.log('')

    // Check for existing custom strategies
    let customStrategies: Array<Record<string, any>> = []
    try {
      await runEngine('workbench-list', [], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          customStrategies = (msg.strategies as Array<Record<string, any>>) || []
        }
      })
    } catch { /* empty */ }

    if (customStrategies.length > 0) {
      this.log(dim('  Your strategies:'))
      this.log('')
      for (let i = 0; i < customStrategies.length; i++) {
        const s = customStrategies[i]
        const filters = (s.filters as string[] || []).join(', ')
        this.log(`    ${cyan(String(i + 1))}  ${bold(String(s.name).padEnd(22))} v${s.version}  ${dim(String(s.description || '').slice(0, 35))}`)
        if (filters) this.log(`       ${dim('filters: ' + filters)}`)
      }
      this.log('')
      this.log(`    ${cyan(String(customStrategies.length + 1))}  ${green('+')} Create new strategy`)
      this.log('')

      const choice = await ask(`  ${cyan('>')} `)
      const idx = parseInt(choice) - 1

      if (idx >= 0 && idx < customStrategies.length) {
        return this.workbench(String(customStrategies[idx].name))
      }
    }

    // Create new strategy flow
    this.log(dim('  Start from a template:'))
    this.log('')
    for (const t of TEMPLATES) {
      this.log(`    ${cyan(t.key)}  ${bold(t.label.padEnd(22))} ${dim('— ' + t.desc)}`)
    }
    this.log('')

    const templateChoice = await ask(`  ${cyan('>')} `)
    const template = TEMPLATES.find(t => t.key === templateChoice)
    if (!template) return this.mainMenu()

    const name = await ask(`\n  ${cyan('Strategy name')} ${dim('(snake_case)')}: `)
    if (!name) return this.mainMenu()

    // Create via engine
    let created = false
    try {
      await runEngine('workbench-create', [name, '--template', template.template], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          created = true
          this.log('')
          this.log(`  ${green('✔')} Created ${bold(name)} from ${dim(template.label)} template`)
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
      return this.mainMenu()
    }

    if (created) {
      this.log('')
      return this.workbench(name)
    }

    return this.mainMenu()
  }

  // ═══════════════════════════════════════════
  //  WORKBENCH — The persistent editing view
  // ═══════════════════════════════════════════

  private async workbench(strategyName: string, pair: string = 'BTC'): Promise<void> {
    // Load config
    let config: Record<string, any> = {}
    try {
      await runEngine('workbench-show', [strategyName], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          config = msg.config as Record<string, any>
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
      return
    }

    if (!config.name) return

    const entry = config.entry as Record<string, any> || {}
    const exit_ = config.exit as Record<string, any> || {}
    const risk = config.risk as Record<string, any> || {}
    const filters = config.filters as Record<string, boolean> || {}
    const entryConds = (entry.conditions as Array<Record<string, any>>) || []
    const exitConds = (exit_.conditions as Array<Record<string, any>>) || []

    // Get last test result
    const lastData: {result: Record<string, any> | null} = {result: null}
    try {
      await runEngine('experiments', [strategyName, '--limit', '1'], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          const exps = (msg.experiments as Array<Record<string, any>>) || []
          if (exps.length > 0) lastData.result = exps[0]
        }
      })
    } catch { /* empty */ }
    const lastResult = lastData.result

    // Render workbench
    const iw = 54
    const wr = boldBoxRow(iw)

    const stratMeta = STRATEGIES.find(s => s.name === strategyName)
    const mktLabel = stratMeta ? ` · ${marketColor(stratMeta.market)}` : ''

    this.log('')
    this.log(boldBoxTop(iw))
    this.log(wr(`  WORKBENCH: ${bold(strategyName)} on ${pair} PERP ${config.timeframe || '1h'}${mktLabel}`))
    this.log(boldBoxDivider(iw))

    if (lastResult) {
      const ret = Number((lastResult.return_pct as number) ?? 0).toFixed(2)
      const sharpe = Number((lastResult.sharpe as number) ?? 0).toFixed(2)
      const trades = (lastResult.num_trades as number) ?? 0
      const win = Number((lastResult.win_rate as number) ?? 0).toFixed(0)
      this.log(wr(`  LAST TEST: ${colorNum(Number(ret), '%')} | Sharpe ${colorNum(Number(sharpe))} | ${trades} trades | ${win}% win`))
    } else {
      this.log(wr(`  ${dim('No tests yet — press [t] to quick test')}`))
    }

    this.log(boldBoxDivider(iw))

    // Entry zone
    this.log(wr(''))
    this.log(wr(`  ${cyan('[1]')} Entry     ${this.formatConditions(entryConds, 'entry')}`))

    // Exit zone
    this.log(wr(''))
    const exitDesc = this.formatConditions(exitConds, 'exit')
    const maxHold = exit_.max_hold || 48
    this.log(wr(`  ${cyan('[2]')} Exit      ${exitDesc}`))
    this.log(wr(`              ${dim(`max hold: ${maxHold} candles`)}`))

    // Risk zone
    this.log(wr(''))
    const sl = risk.stop_loss ? `${(risk.stop_loss * 100).toFixed(1)}%` : '2.0%'
    const lev = risk.leverage || 2.0
    const rpt = risk.risk_per_trade ? `${(risk.risk_per_trade * 100).toFixed(1)}%` : '2.0%'
    this.log(wr(`  ${cyan('[3]')} Risk      SL: ${sl}  Size: ${rpt}  Lev: ${lev}x`))

    // Filters zone
    this.log(wr(''))
    const filterList = Object.entries(filters)
      .map(([k, v]) => `${v ? green('☑') : dim('☐')} ${k.replace(/_/g, ' ')}`)
      .join('  ')
    this.log(wr(`  ${cyan('[4]')} Filters   ${filterList || dim('none')}`))

    this.log(wr(''))
    this.log(boldBoxDivider(iw))
    this.log(wr(`  ${cyan('[t]')} Quick test  ${cyan('[T]')} Full validate  ${cyan('[h]')} History`))
    this.log(wr(`  ${cyan('[p]')} Change pair ${cyan('[m]')} Mixer          ${cyan('[q]')} Back`))
    this.log(boldBoxBottom(iw))
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    switch (choice) {
      case '1': return this.editEntry(strategyName, pair)
      case '2': return this.editExit(strategyName, pair)
      case '3': return this.editRisk(strategyName, pair)
      case '4': return this.editFilters(strategyName, pair)
      case 't': return this.runQuickTest(strategyName, pair)
      case 'T': return this.runFullValidate(strategyName, pair)
      case 'h': return this.showHistory(strategyName, pair)
      case 'p':
        const newPair = (await ask(`  ${cyan('Ticker')}: `) || pair).replace('-PERP', '').replace('-perp', '').toUpperCase()
        return this.workbench(strategyName, newPair)
      case 'm': return this.mixerMenu(strategyName, pair)
      case 'q': return this.buildMenu()
      default: return this.workbench(strategyName, pair)
    }
  }

  private formatConditions(conds: Array<Record<string, any>>, _type: string): string {
    const validConds = conds.filter(c => !(c.indicator as string)?.startsWith('_'))
    if (validConds.length === 0) return dim('(none configured)')
    // Show first condition inline, rest are visible in the edit menu
    const c = validConds[0]
    const ind = c.indicator as string
    const op = c.op as string
    const val = c.value !== undefined ? c.value : c.ref
    const side = c.side ? dim(` [${c.side}]`) : ''
    let result = `${ind} ${op} ${val}${side}`
    if (validConds.length > 1) result += dim(` +${validConds.length - 1} more`)
    return result
  }

  // ═══════════════════════════════════════════
  //  WORKBENCH — Zone editors
  // ═══════════════════════════════════════════

  private async editEntry(strategyName: string, pair: string): Promise<void> {
    let config: Record<string, any> = {}
    await runEngine('workbench-show', [strategyName], (msg: EngineMessage) => {
      if (msg.type === 'result') config = msg.config as Record<string, any>
    })

    const entry = config.entry as Record<string, any> || {}
    const conds = (entry.conditions as Array<Record<string, any>>) || []

    this.log('')
    this.log(`  ${bold('ENTRY CONDITIONS')} for ${bold(strategyName)}`)
    this.log('')
    this.log(`  Current:`)
    if (conds.length === 0) {
      this.log(`    ${dim('(no conditions set)')}`)
    } else {
      for (let i = 0; i < conds.length; i++) {
        const c = conds[i]
        this.log(`    ${dim(String(i + 1) + '.')} ${c.indicator} ${c.op} ${c.value ?? c.ref}${c.side ? dim(` [${c.side}]`) : ''}`)
      }
    }
    this.log('')
    this.log(`    ${cyan('a')}  Add a condition`)
    this.log(`    ${cyan('r')}  Remove a condition`)
    this.log(`    ${cyan('d')}  Change direction ${dim(`(current: ${entry.direction || 'both'})`)}`)
    this.log(`    ${cyan('b')}  Back to workbench`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    if (choice === 'a') {
      const newCond = await this.guidedConditionPicker(pair)
      if (newCond) {
        conds.push(newCond)
        entry.conditions = conds
        config.entry = entry
        const desc = `added entry: ${newCond.indicator} ${newCond.op} ${newCond.value ?? newCond.ref}`
        await this.saveAndRegenerate(strategyName, config, pair, desc)
      }
    } else if (choice === 'r' && conds.length > 0) {
      const idx = parseInt(await ask(`  ${cyan('Remove #')}: `)) - 1
      if (idx >= 0 && idx < conds.length) {
        const removed = conds.splice(idx, 1)[0]
        entry.conditions = conds
        config.entry = entry
        await this.saveAndRegenerate(strategyName, config, pair, `removed entry: ${removed.indicator}`)
      }
    } else if (choice === 'd') {
      const dir = await ask(`  ${cyan('Direction')} ${dim('(both/long_only/short_only)')}: `)
      if (dir) {
        entry.direction = dir
        config.entry = entry
        await this.saveAndRegenerate(strategyName, config, pair, `direction → ${dir}`)
      }
    }

    return this.workbench(strategyName, pair)
  }

  private async editExit(strategyName: string, pair: string): Promise<void> {
    let config: Record<string, any> = {}
    await runEngine('workbench-show', [strategyName], (msg: EngineMessage) => {
      if (msg.type === 'result') config = msg.config as Record<string, any>
    })

    const exit_ = config.exit as Record<string, any> || {}
    const conds = (exit_.conditions as Array<Record<string, any>>) || []

    this.log('')
    this.log(`  ${bold('EXIT CONDITIONS')} for ${bold(strategyName)}`)
    this.log('')
    this.log(`  Current:`)
    this.log(`    ${dim('•')} Max hold: ${exit_.max_hold || 48} candles`)
    for (let i = 0; i < conds.length; i++) {
      const c = conds[i]
      this.log(`    ${dim(String(i + 1) + '.')} ${c.indicator} ${c.op} ${c.value ?? c.ref}${c.side ? dim(` [${c.side}]`) : ''}`)
    }
    this.log('')
    this.log(`    ${cyan('a')}  Add exit condition`)
    this.log(`    ${cyan('r')}  Remove exit condition`)
    this.log(`    ${cyan('h')}  Change max hold`)
    this.log(`    ${cyan('b')}  Back`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    if (choice === 'a') {
      const newCond = await this.guidedConditionPicker(pair, true)
      if (newCond) {
        if (newCond.indicator === '_max_hold') {
          // Special case: max hold is a config property, not a condition
          exit_.max_hold = newCond.value
          config.exit = exit_
          await this.saveAndRegenerate(strategyName, config, pair, `max hold → ${newCond.value}`)
        } else {
          conds.push(newCond)
          exit_.conditions = conds
          config.exit = exit_
          await this.saveAndRegenerate(strategyName, config, pair, `added exit: ${newCond.indicator} ${newCond.op} ${newCond.value}`)
        }
      }
    } else if (choice === 'r' && conds.length > 0) {
      const idx = parseInt(await ask(`  ${cyan('Remove #')}: `)) - 1
      if (idx >= 0 && idx < conds.length) {
        conds.splice(idx, 1)
        exit_.conditions = conds
        config.exit = exit_
        await this.saveAndRegenerate(strategyName, config, pair, `removed exit condition`)
      }
    } else if (choice === 'h') {
      const hold = await ask(`  ${cyan('Max hold (candles)')}: `)
      if (hold) {
        exit_.max_hold = parseInt(hold)
        config.exit = exit_
        await this.saveAndRegenerate(strategyName, config, pair, `max hold → ${hold}`)
      }
    }

    return this.workbench(strategyName, pair)
  }

  private async editRisk(strategyName: string, pair: string): Promise<void> {
    let config: Record<string, any> = {}
    await runEngine('workbench-show', [strategyName], (msg: EngineMessage) => {
      if (msg.type === 'result') config = msg.config as Record<string, any>
    })

    const risk = config.risk as Record<string, any> || {}

    this.log('')
    this.log(`  ${bold('RISK SETTINGS')} for ${bold(strategyName)}`)
    this.log('')
    this.log(`    ${cyan('1')}  Stop loss:       ${bold(String((risk.stop_loss * 100).toFixed(1) + '%'))}`)
    this.log(`    ${cyan('2')}  Risk per trade:   ${bold(String((risk.risk_per_trade * 100).toFixed(1) + '%'))}`)
    this.log(`    ${cyan('3')}  Leverage:         ${bold(String(risk.leverage + 'x'))}`)
    this.log(`    ${cyan('b')}  Back`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    if (choice === '1') {
      const val = await ask(`  ${cyan('Stop loss %')}: `)
      if (val) {
        const oldSl = (risk.stop_loss * 100).toFixed(1)
        risk.stop_loss = parseFloat(val) / 100
        config.risk = risk
        await this.saveAndRegenerate(strategyName, config, pair, `stop loss ${oldSl}% → ${val}%`)
      }
    } else if (choice === '2') {
      const val = await ask(`  ${cyan('Risk per trade %')}: `)
      if (val) {
        risk.risk_per_trade = parseFloat(val) / 100
        config.risk = risk
        await this.saveAndRegenerate(strategyName, config, pair, `risk per trade → ${val}%`)
      }
    } else if (choice === '3') {
      const val = await ask(`  ${cyan('Leverage')}: `)
      if (val) {
        risk.leverage = parseFloat(val)
        config.risk = risk
        await this.saveAndRegenerate(strategyName, config, pair, `leverage → ${val}x`)
      }
    }

    return this.workbench(strategyName, pair)
  }

  private async editFilters(strategyName: string, pair: string): Promise<void> {
    let config: Record<string, any> = {}
    await runEngine('workbench-show', [strategyName], (msg: EngineMessage) => {
      if (msg.type === 'result') config = msg.config as Record<string, any>
    })

    const filters = config.filters as Record<string, boolean> || {}
    const available = [
      {key: 'hmm_filter', desc: 'HMM regime filter — skip crisis markets (self-contained, no dependency)'},
      {key: 'rsi_confirmation', desc: 'RSI confirmation — oversold/overbought gates'},
      {key: 'volume_filter', desc: 'Volume filter — require 1.5x avg volume'},
      {key: 'adx_trend', desc: 'ADX trend filter — only trade when trending (>25)'},
    ]

    this.log('')
    this.log(`  ${bold('FILTERS')} for ${bold(strategyName)}`)
    this.log('')
    for (let i = 0; i < available.length; i++) {
      const f = available[i]
      const on = filters[f.key] === true
      this.log(`    ${cyan(String(i + 1))}  ${on ? green('☑') : dim('☐')}  ${bold(f.key.padEnd(22))} ${dim(f.desc)}`)
    }
    this.log(`    ${cyan('b')}  Back`)
    this.log('')

    const choice = await ask(`  ${cyan('Toggle #')} `)
    const idx = parseInt(choice) - 1

    if (idx >= 0 && idx < available.length) {
      const key = available[idx].key
      filters[key] = !filters[key]
      config.filters = filters
      const action = filters[key] ? 'enabled' : 'disabled'
      await this.saveAndRegenerate(strategyName, config, pair, `${action} ${key}`)
    }

    return this.workbench(strategyName, pair)
  }

  // ═══════════════════════════════════════════
  //  GUIDED CONDITION PICKER — with live stats
  // ═══════════════════════════════════════════

  private async guidedConditionPicker(pair: string, isExit: boolean = false): Promise<Record<string, any> | null> {
    // Step 1: Category
    this.log('')
    this.log(`  ${bold(isExit ? 'ADD EXIT CONDITION' : 'ADD ENTRY CONDITION')}`)
    this.log('')
    this.log(`  ${dim('Pick a category:')}`)
    this.log('')
    this.log(`    ${cyan('1')}  ${bold('Funding')}        ${dim('funding rate thresholds')}`)
    this.log(`    ${cyan('2')}  ${bold('Price')}          ${dim('vs EMA, vs VWAP')}`)
    this.log(`    ${cyan('3')}  ${bold('Momentum')}       ${dim('RSI, ADX')}`)
    this.log(`    ${cyan('4')}  ${bold('Volume')}         ${dim('volume spike detection')}`)
    if (isExit) {
      this.log(`    ${cyan('5')}  ${bold('Time')}           ${dim('max hold candles')}`)
    }
    this.log('')

    const cat = await ask(`  ${cyan('>')} `)

    // Fetch live stats for this pair
    let stats: Record<string, any> = {}
    this.log(dim('  Loading market stats...'))
    try {
      await runEngine('indicator-stats', ['--pair', pair], (msg: EngineMessage) => {
        if (msg.type === 'result') stats = msg
      })
    } catch { /* stats are optional enhancement */ }

    switch (cat) {
      case '1': return this.pickFundingCondition(stats, isExit)
      case '2': return this.pickPriceCondition(stats, isExit)
      case '3': return this.pickMomentumCondition(stats, isExit)
      case '4': return this.pickVolumeCondition(stats)
      case '5': if (isExit) return this.pickTimeCondition()
      default: return null
    }
  }

  private async pickFundingCondition(stats: Record<string, any>, isExit: boolean): Promise<Record<string, any> | null> {
    const fs = stats.funding_rate as Record<string, any> | undefined

    this.log('')
    if (isExit) {
      this.log(`  ${bold('FUNDING EXIT')}`)
      this.log('')
      this.log(`    ${cyan('1')}  Funding normalizes    ${dim('exit when rate drops below threshold')}`)
      this.log('')
    } else {
      this.log(`  ${bold('FUNDING ENTRY')}`)
      this.log('')
      this.log(`    ${cyan('1')}  Funding rate extreme   ${dim('enter when |rate| exceeds threshold')}`)
      this.log(`    ${cyan('2')}  Funding rate positive   ${dim('enter when shorts earn (rate > 0)')}`)
      this.log(`    ${cyan('3')}  Funding rate negative   ${dim('enter when longs earn (rate < 0)')}`)
      this.log('')
    }

    const choice = await ask(`  ${cyan('>')} `)

    // Show live stats
    if (fs) {
      this.log('')
      this.log(`  ${dim(`${stats.pair} funding — last ${stats.candles} candles:`)}`)

      // Visual gauge
      const min = fs.min as number
      const max = fs.max as number
      const p75 = fs.p75 as number
      const p90 = fs.p90 as number
      const rec = fs.recommended as number

      this.log(`    ${dim('Min')}      ${dim(String(min))}`)
      this.log(`    ${dim('Median')}   ${dim(String(fs.median))}`)
      this.log(`    ${dim('75th')}     ${cyan(String(p75))}`)
      this.log(`    ${dim('90th')}     ${yellow(String(p90))}`)
      this.log(`    ${dim('Max')}      ${dim(String(max))}`)
      this.log(`    ${dim(`${fs.positive_pct}% of the time, longs pay shorts`)}`)
      this.log('')
      this.log(`  ${green('★')} Recommended: ${bold(String(rec))} ${dim('(75th percentile of |rate|)')}`)
    }

    if (isExit) {
      const defaultVal = fs ? fs.median : 0.000003
      const val = await ask(`\n  ${cyan('Exit below')} ${dim(`(${defaultVal})`)}: `)
      const threshold = val ? parseFloat(val) : defaultVal
      return {indicator: 'funding_rate', op: 'abs_below', value: threshold}
    }

    if (choice === '1') {
      const defaultVal = fs ? fs.recommended : 0.000015
      const val = await ask(`\n  ${cyan('Threshold')} ${dim(`(${defaultVal})`)}: `)
      const threshold = val ? parseFloat(val) : defaultVal

      this.log('')
      this.log(`  ${dim('When funding exceeds this threshold:')}`)
      this.log(`    ${cyan('1')}  ${bold('SHORT')} when positive ${dim('— shorts earn (recommended)')}`)
      this.log(`    ${cyan('2')}  ${bold('LONG')} when negative ${dim('— longs earn (recommended)')}`)
      this.log(`    ${cyan('3')}  ${bold('Both')} directions`)
      this.log('')
      const sideChoice = await ask(`  ${cyan('>')} `)

      if (sideChoice === '1') {
        return {indicator: 'funding_rate', op: '>', value: threshold, side: 'short'}
      } else if (sideChoice === '2') {
        return {indicator: 'funding_rate', op: '<', value: -threshold, side: 'long'}
      } else {
        // Both — add as two conditions? Just return the positive side, user can add more
        return {indicator: 'funding_rate', op: '>', value: threshold, side: 'short'}
      }
    } else if (choice === '2') {
      const defaultVal = fs ? fs.recommended : 0.000015
      const val = await ask(`\n  ${cyan('Min positive rate')} ${dim(`(${defaultVal})`)}: `)
      return {indicator: 'funding_rate', op: '>', value: val ? parseFloat(val) : defaultVal, side: 'short'}
    } else if (choice === '3') {
      const defaultVal = fs ? -(fs.recommended as number) : -0.000015
      const val = await ask(`\n  ${cyan('Max negative rate')} ${dim(`(${defaultVal})`)}: `)
      return {indicator: 'funding_rate', op: '<', value: val ? parseFloat(val) : defaultVal, side: 'long'}
    }
    return null
  }

  private async pickPriceCondition(stats: Record<string, any>, isExit: boolean): Promise<Record<string, any> | null> {
    this.log('')
    this.log(`  ${bold('PRICE CONDITIONS')}`)
    this.log('')
    this.log(`    ${cyan('1')}  Price above EMA      ${dim('trend filter — only trade with trend')}`)
    this.log(`    ${cyan('2')}  Price below EMA      ${dim('mean reversion — buy the dip')}`)
    this.log(`    ${cyan('3')}  VWAP deviation       ${dim('extreme deviation from fair value')}`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    if (choice === '1' || choice === '2') {
      // EMA period selection
      const emaStats = stats.ema_100 as Record<string, any> | undefined

      this.log('')
      this.log(`  ${dim('EMA period:')}`)
      this.log(`    ${cyan('1')}  EMA 50   ${dim('— faster, more responsive')}`)
      this.log(`    ${cyan('2')}  EMA 100  ${dim('— balanced (default)')}`)
      this.log(`    ${cyan('3')}  EMA 200  ${dim('— slower, stronger filter')}`)
      this.log('')

      if (emaStats) {
        this.log(`  ${dim(`${stats.pair}: price above EMA 100 ${emaStats.pct_above}% of the time`)}`)
        this.log('')
      }

      const periodChoice = await ask(`  ${cyan('>')} ${dim('(2)')}: `) || '2'
      const period = periodChoice === '1' ? 50 : periodChoice === '3' ? 200 : 100
      // Price above EMA = bullish = use as filter for SHORT funding trades (price overextended)
      // Price below EMA = bearish = use as filter for LONG funding trades (price oversold)
      const op = choice === '1' ? '>' : '<'
      const side = choice === '1' ? 'short' : 'long'

      return {indicator: 'price', op, ref: `ema_${period}`, side}
    } else if (choice === '3') {
      const vs = stats.vwap_zscore as Record<string, any> | undefined

      if (vs) {
        this.log('')
        this.log(`  ${dim(`${stats.pair} VWAP z-score distribution:`)}`)
        this.log(`    ${dim('5th')}    ${dim(String(vs.p5))}σ`)
        this.log(`    ${dim('95th')}   ${dim(String(vs.p95))}σ`)
        this.log(`    ${dim(`Beyond ±2σ: ${vs.pct_beyond_2}% of candles`)}`)
        this.log(`    ${dim(`Beyond ±3σ: ${vs.pct_beyond_3}% of candles`)}`)
        this.log('')
        this.log(`  ${green('★')} Recommended entry: ${bold(`±${vs.recommended_entry}σ`)} ${dim('(95th percentile)')}`)
      }

      const defaultDev = vs ? vs.recommended_entry : 2.5
      const val = await ask(`\n  ${cyan('Entry deviation (σ)')} ${dim(`(${defaultDev})`)}: `)
      const dev = val ? parseFloat(val) : defaultDev

      return {indicator: 'vwap_zscore', op: isExit ? 'abs_below' : '<', value: isExit ? dev : -dev, side: 'long'}
    }
    return null
  }

  private async pickMomentumCondition(stats: Record<string, any>, isExit: boolean): Promise<Record<string, any> | null> {
    this.log('')
    this.log(`  ${bold('MOMENTUM CONDITIONS')}`)
    this.log('')
    this.log(`    ${cyan('1')}  RSI oversold/overbought  ${dim('momentum extremes')}`)
    this.log(`    ${cyan('2')}  ADX trend strength       ${dim('only trade when trending')}`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    if (choice === '1') {
      const rs = stats.rsi as Record<string, any> | undefined

      if (rs) {
        this.log('')
        this.log(`  ${dim(`${stats.pair} RSI (14) distribution:`)}`)
        this.log(`    ${dim('10th')}   ${dim(String(rs.p10))}`)
        this.log(`    ${dim('25th')}   ${dim(String(rs.p25))}`)
        this.log(`    ${dim('Mean')}   ${dim(String(rs.mean))}`)
        this.log(`    ${dim('75th')}   ${dim(String(rs.p75))}`)
        this.log(`    ${dim('90th')}   ${dim(String(rs.p90))}`)
        this.log(`    ${dim(`Below 30: ${rs.pct_below_30}% · Above 70: ${rs.pct_above_70}%`)}`)
        this.log('')
        this.log(`  ${green('★')} Recommended: oversold < ${bold(String(rs.recommended_oversold))} · overbought > ${bold(String(rs.recommended_overbought))}`)
      }

      this.log('')
      this.log(`    ${cyan('1')}  RSI oversold ${dim('— enter LONG when RSI < threshold')}`)
      this.log(`    ${cyan('2')}  RSI overbought ${dim('— enter SHORT when RSI > threshold')}`)
      this.log('')

      const rsiChoice = await ask(`  ${cyan('>')} `)

      if (rsiChoice === '1') {
        const def = rs ? rs.recommended_oversold : 40
        const val = await ask(`  ${cyan('RSI below')} ${dim(`(${def})`)}: `)
        return {indicator: 'rsi', op: '<', value: val ? parseFloat(val) : def, side: 'long'}
      } else if (rsiChoice === '2') {
        const def = rs ? rs.recommended_overbought : 60
        const val = await ask(`  ${cyan('RSI above')} ${dim(`(${def})`)}: `)
        return {indicator: 'rsi', op: '>', value: val ? parseFloat(val) : def, side: 'short'}
      }
    } else if (choice === '2') {
      const as = stats.adx as Record<string, any> | undefined

      if (as) {
        this.log('')
        this.log(`  ${dim(`${stats.pair} ADX (14):`)}`)
        this.log(`    ${dim(`Mean: ${as.mean} · Median: ${as.median}`)}`)
        this.log(`    ${dim(`Above 25 (trending): ${as.pct_above_25}%`)}`)
        this.log(`    ${dim(`Above 40 (strong trend): ${as.pct_above_40}%`)}`)
        this.log('')
        this.log(`  ${green('★')} Recommended: ${bold('> 25')} ${dim('(standard trend threshold)')}`)
      }

      const def = 25
      const val = await ask(`\n  ${cyan('ADX above')} ${dim(`(${def})`)}: `)
      return {indicator: 'adx', op: '>', value: val ? parseFloat(val) : def}
    }
    return null
  }

  private async pickVolumeCondition(stats: Record<string, any>): Promise<Record<string, any> | null> {
    const vs = stats.volume as Record<string, any> | undefined

    this.log('')
    this.log(`  ${bold('VOLUME CONDITIONS')}`)

    if (vs) {
      this.log('')
      this.log(`  ${dim(`${stats.pair} volume ratio (vs 20-period avg):`)}`)
      this.log(`    ${dim(`Median: ${vs.median}x · 75th: ${vs.p75}x · 90th: ${vs.p90}x · 95th: ${vs.p95}x`)}`)
      this.log('')
      this.log(`  ${green('★')} Recommended: ${bold(`> ${vs.recommended}x`)} ${dim('(above average volume)')}`)
    }

    const def = vs ? vs.recommended : 1.5
    const val = await ask(`\n  ${cyan('Volume above')} ${dim(`(${def}x avg)`)}: `)
    return {indicator: 'vol_ratio', op: '>', value: val ? parseFloat(val) : def}
  }

  private async pickTimeCondition(): Promise<Record<string, any> | null> {
    this.log('')
    this.log(`  ${bold('TIME EXIT')}`)
    this.log(`  ${dim('Close position after N candles regardless of conditions.')}`)
    this.log('')
    this.log(`    ${dim('Common values: 6 (quick), 24 (1 day), 48 (2 days), 72 (3 days)')}`)
    const val = await ask(`\n  ${cyan('Max hold candles')} ${dim('(48)')}: `)
    // This is handled via max_hold in exit config, not as a condition
    // Return a special marker that editExit handles
    return {indicator: '_max_hold', op: '=', value: val ? parseInt(val, 10) : 48}
  }

  private async saveAndRegenerate(strategyName: string, config: Record<string, any>, pair: string, changeDesc: string): Promise<void> {
    try {
      await runEngine('workbench-update', [strategyName, JSON.stringify(config)], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          this.log(`  ${green('✔')} Updated — ${dim(changeDesc)}`)
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
    }

    // Auto quick-test after every change
    const doTest = await ask(`  ${dim('Quick test? (y/n)')}: `)
    if (doTest.toLowerCase() === 'y' || doTest === '') {
      await this.runQuickTest(strategyName, pair, changeDesc)
    }
  }

  // ═══════════════════════════════════════════
  //  WORKBENCH — Quick test & history
  // ═══════════════════════════════════════════

  private async runQuickTest(strategyName: string, pair: string, changeDesc: string = ''): Promise<void> {
    this.log('')
    this.log(dim(`  Testing ${strategyName} on ${pair}...`))

    const args = [strategyName, '--pair', pair]
    if (changeDesc) args.push('--change', changeDesc)

    try {
      await runEngine('quick-test', args, (msg: EngineMessage) => {
        if (msg.type === 'progress') {
          this.log(`  ${dim(String(msg.msg))}`)
        } else if (msg.type === 'result') {
          const ret = msg.return_pct as number
          const sharpe = msg.sharpe as number
          const trades = msg.num_trades as number
          const win = msg.win_rate as number
          const dd = msg.max_drawdown as number
          const pf = msg.profit_factor as number

          this.log('')
          this.log(`  ${bold('Quick test:')} ${colorNum(ret, '%')}, ${trades} trades, ${win}% win, ${colorNum(dd, '%')} DD, Sharpe ${colorNum(sharpe)}`)

          // Show delta if available
          if (msg.has_previous) {
            const delta = msg.delta as Record<string, number>
            const parts: string[] = []
            if (delta.return_pct) parts.push(`return ${delta.return_pct > 0 ? '↑' : '↓'}${Math.abs(delta.return_pct).toFixed(1)}%`)
            if (delta.sharpe) parts.push(`Sharpe ${delta.sharpe > 0 ? '↑' : '↓'}${Math.abs(delta.sharpe).toFixed(2)}`)
            if (delta.num_trades) parts.push(`trades ${delta.num_trades > 0 ? '↑' : '↓'}${Math.abs(delta.num_trades)}`)
            if (delta.max_drawdown) parts.push(`DD ${delta.max_drawdown > 0 ? '↑' : '↓'}${Math.abs(delta.max_drawdown).toFixed(1)}%`)
            if (parts.length > 0) {
              this.log(`  ${dim('vs last:')} ${parts.join(', ')}`)
            }
          }
          this.log('')
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
    }

    return this.workbench(strategyName, pair)
  }

  private async runFullValidate(strategyName: string, pair: string): Promise<void> {
    this.log('')
    await this.runPipeline(strategyName, pair)
    return this.workbench(strategyName, pair)
  }

  private async showHistory(strategyName: string, pair: string): Promise<void> {
    this.log('')
    this.log(`  ${bold('EXPERIMENT LOG:')} ${strategyName}`)
    this.log('')

    try {
      await runEngine('experiments', [strategyName, '--limit', '15'], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          const exps = (msg.experiments as Array<Record<string, any>>) || []
          if (exps.length === 0) {
            this.log(dim('  No experiments yet. Press [t] in the workbench to start.'))
          } else {
            this.log(`  ${dim('#'.padEnd(4))} ${dim('v'.padEnd(4))} ${dim('Pair'.padEnd(6))} ${dim('Return'.padEnd(10))} ${dim('Sharpe'.padEnd(9))} ${dim('Trades'.padEnd(8))} ${dim('Change')}`)
            this.log(`  ${dim('─'.repeat(65))}`)
            for (const e of exps) {
              const ret = colorNum(e.return_pct as number, '%')
              const cleanRet = String(e.return_pct as number).slice(0, 6)
              this.log(`  ${String(e.id).padEnd(4)} v${String(e.version).padEnd(3)} ${String(e.pair).padEnd(6)} ${ret.padEnd(18)} ${String((e.sharpe as number)?.toFixed(2) || '—').padEnd(9)} ${String(e.num_trades).padEnd(8)} ${dim(String(e.change_description || ''))}`)
            }
          }
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
    }

    this.log('')
    this.log(`    ${cyan('r')}  Revert to a version`)
    this.log(`    ${cyan('b')}  Back to workbench`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)
    if (choice === 'r') {
      const expId = await ask(`  ${cyan('Experiment #')}: `)
      if (expId) {
        try {
          await runEngine('experiment-revert', [expId], (msg: EngineMessage) => {
            if (msg.type === 'result') {
              this.log(`  ${green('✔')} Reverted to experiment #${expId} (now v${msg.version})`)
            } else if (msg.type === 'error') {
              this.log(`  ${red('✘')} ${msg.msg}`)
            }
          })
        } catch (e: any) {
          this.log(`  ${red('✘')} ${e.message}`)
        }
      }
    }

    return this.workbench(strategyName, pair)
  }

  // ═══════════════════════════════════════════
  //  WORKBENCH — Strategy Mixer
  // ═══════════════════════════════════════════

  private async mixerMenu(strategyName: string, pair: string): Promise<void> {
    this.log('')
    this.log(`  ${bold('STRATEGY MIXER')} — combine components from validated strategies`)
    this.log('')

    // Get current config
    let config: Record<string, any> = {}
    try {
      await runEngine('workbench-show', [strategyName], (msg: EngineMessage) => {
        if (msg.type === 'result') config = msg.config as Record<string, any>
      })
    } catch { /* empty */ }

    const filters = config.filters as Record<string, boolean> || {}

    this.log(`  Your base: ${bold(strategyName)}`)
    this.log('')
    this.log(`  ${dim('FILTERS (toggle to add/remove):')}`)
    this.log('')

    const filterOptions = [
      {key: 'hmm_filter', label: 'HMM crisis filter', source: 'workbench-builtin', desc: 'skip trades during crisis regimes (HMM, self-contained)'},
      {key: 'rsi_confirmation', label: 'RSI confirmation', source: 'workbench-builtin', desc: 'require RSI < 40 for longs, > 60 for shorts'},
      {key: 'volume_filter', label: 'Volume spike', source: 'custom', desc: 'require 1.5x average volume'},
      {key: 'adx_trend', label: 'ADX trend', source: 'custom', desc: 'only trade when ADX > 25'},
    ]

    for (let i = 0; i < filterOptions.length; i++) {
      const f = filterOptions[i]
      const on = filters[f.key] === true
      this.log(`    ${cyan(String(i + 1))}  ${on ? green('☑') : dim('☐')} From ${bold(f.source)}: ${f.desc}`)
    }

    this.log('')
    this.log(`  ${dim('Benchmarks — your strategy vs validated:')}`)
    this.log('')
    for (const s of STRATEGIES) {
      this.log(`    ${green('★')} ${s.name.padEnd(22)} ${colorNum(s.ret, '%').padEnd(18)} Sharpe ${s.sharpe}`)
    }

    this.log('')
    this.log(`    ${cyan('#')}  Toggle a filter`)
    this.log(`    ${cyan('t')}  Quick test this combination`)
    this.log(`    ${cyan('b')}  Back to workbench`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)
    const idx = parseInt(choice) - 1

    if (idx >= 0 && idx < filterOptions.length) {
      const key = filterOptions[idx].key
      filters[key] = !filters[key]
      config.filters = filters
      const action = filters[key] ? 'enabled' : 'disabled'
      await this.saveAndRegenerate(strategyName, config, pair, `mixer: ${action} ${key}`)
    } else if (choice === 't') {
      await this.runQuickTest(strategyName, pair, 'mixer test')
    }

    if (choice === 'b') {
      return this.workbench(strategyName, pair)
    }
    return this.mixerMenu(strategyName, pair)
  }

  // ═══════════════════════════════════════════
  //  4. OPTIMIZE — Parameter sweep + apply
  // ═══════════════════════════════════════════

  private async optimizeMenu(): Promise<void> {
    this.log('')
    this.log(`  ${bold('╔═══════════════════════════════════════════╗')}`)
    this.log(`  ${bold('║          Strategy Optimizer               ║')}`)
    this.log(`  ${bold('╚═══════════════════════════════════════════╝')}`)

    const strategy = await this.pickStrategy('Strategy to optimize')
    if (!strategy) return this.mainMenu()

    const pair = (await ask(`  ${cyan('Ticker')} ${dim('(BTC)')}: `) || 'BTC').replace('-PERP', '').replace('-perp', '').toUpperCase()

    this.log('')
    this.log(dim(`  Running parameter sweep on ${strategy}...`))
    this.log('')

    // Run sweep via engine directly so we can capture the best params
    let bestParams: Record<string, any> | null = null
    let bestMetrics: Record<string, any> | null = null
    const isValidated = STRATEGIES.some(s => s.name === strategy)

    try {
      await runEngine('sweep', [strategy, '--pair', pair, '--top', '5'], (msg: EngineMessage) => {
        if (msg.type === 'progress' && msg.msg) {
          // Compact progress: just combo count + ETA
          const msgStr = String(msg.msg)
          const match = msgStr.match(/Combo (\d+)\/(\d+)(.*?ETA \S+)?/)
          if (match) {
            const eta = match[3] ? match[3].trim() : ''
            process.stdout.write(`\x1b[2K\r  ${dim(`Combo ${match[1]}/${match[2]}${eta ? ' — ' + eta : ''}`)}`)
          } else {
            process.stdout.write(`\x1b[2K\r  ${dim(msgStr)}`)
          }
        } else if (msg.type === 'result') {
          process.stdout.write('\x1b[2K\r')
          const topEntries = msg.top as Array<{params: Record<string, any>; metrics: Record<string, any>}>
          const total = msg.total_combos as number
          const completed = msg.completed as number

          this.log(dim(`  Tested ${completed}/${total} combinations`))
          this.log('')

          if (topEntries && topEntries.length > 0) {
            bestParams = topEntries[0].params
            bestMetrics = topEntries[0].metrics

            this.log(dim('  ── Top Results ──'))
            this.log('')
            for (let i = 0; i < Math.min(topEntries.length, 5); i++) {
              const e = topEntries[i]
              const m = e.metrics
              const ret = m.total_return_pct as number
              const sharpe = m.sharpe_ratio as number
              const marker = i === 0 ? green('★') : dim(String(i + 1))
              this.log(`  ${marker}  ${colorNum(ret, '%').padEnd(22)} Sharpe ${colorNum(sharpe)}`)
              const paramStr = Object.entries(e.params).map(([k, v]) => `${k}=${v}`).join(', ')
              this.log(`     ${dim(paramStr)}`)
              this.log('')
            }
          }
        } else if (msg.type === 'error') {
          process.stdout.write('\x1b[2K\r')
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
      return this.mainMenu()
    }

    if (!bestParams) {
      this.log(dim('  No results from sweep.'))
      return this.mainMenu()
    }

    this.log(`  ${bold('What next?')}`)
    this.log(`    ${cyan('1')}  Validate the top config with full research pipeline`)
    this.log(`    ${cyan('2')}  Run another sweep with different settings`)
    this.log(`    ${cyan('3')}  Back to main menu`)
    this.log('')

    const next = await ask(`  ${cyan('>')} `)
    if (next === '1') {
      // Run the research pipeline with config overrides — no new strategy file needed
      this.log('')
      this.log(dim(`  Validating ${strategy} with optimized parameters...`))
      this.log('')

      const overridesJson = JSON.stringify(bestParams)
      let finalGrade = ''

      const engineArgs = [strategy, '--pair', pair, '--config-overrides', overridesJson]
      await runEngine('research', engineArgs, (msg: EngineMessage) => {
        if (msg.type === 'step') {
          this.log(`  ${dim(`Step ${msg.step}/5`)} ${msg.msg}`)
        } else if (msg.type === 'step_done') {
          this.log(`  ${green('✔')} ${msg.msg}`)
        } else if (msg.type === 'progress' && msg.msg) {
          this.log(`  ${dim(String(msg.msg))}`)
        } else if (msg.type === 'result') {
          finalGrade = msg.grade as string
          this.renderGradedResult(msg)
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })

      // After validation, offer to save
      if (finalGrade === 'A' || finalGrade === 'B') {
        this.log(`  ${green('★')} Optimization validated! Grade ${gradeColor(finalGrade)}`)
        this.log('')
        this.log(`    ${cyan('1')}  Save as a new strategy`)
        this.log(`    ${cyan('2')}  Back to main menu`)
        this.log('')

        const saveChoice = await ask(`  ${cyan('>')} `)
        if (saveChoice === '1') {
          const saveName = await ask(`  ${cyan('Strategy name')} ${dim('(snake_case)')}: `)
          if (saveName) {
            await this.saveOptimizedStrategy(strategy, saveName, bestParams!)
          }
        }
      } else {
        this.log(`  ${dim('Optimization did not pass validation. Try different parameters.')}`)
      }

      return this.mainMenu()
    } else if (next === '2') {
      return this.optimizeMenu()
    }
    return this.mainMenu()
  }

  /**
   * Save an optimized strategy by copying the original .py file and replacing config defaults.
   */
  private async saveOptimizedStrategy(baseStrategy: string, newName: string, params: Record<string, any>): Promise<void> {
    // Use the Python engine to create the strategy file
    const paramsJson = JSON.stringify(params)
    try {
      await runEngine('save-optimized', [baseStrategy, newName, paramsJson], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          this.log(`  ${green('✔')} Saved ${bold(newName)} with optimized parameters`)
          this.log(`  ${dim(`File: ${msg.path}`)}`)
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
    }
  }

  // ═══════════════════════════════════════════
  //  5. COMPARE — Head-to-head
  // ═══════════════════════════════════════════

  private async compareMenu(): Promise<void> {
    this.log('')
    this.log(`  ${bold('╔═══════════════════════════════════════════╗')}`)
    this.log(`  ${bold('║          Strategy Comparison              ║')}`)
    this.log(`  ${bold('╚═══════════════════════════════════════════╝')}`)
    this.log('')

    // Picker — dynamic from registry + workbench
    this.log(`    ${cyan('1')}  Compare all registered strategies`)
    this.log(`    ${cyan('2')}  Pick specific strategies`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    let strategies: string
    if (choice === '1') {
      // Fetch full registry, comma-join
      let allNames: string[] = []
      try {
        await runEngine('strategies', [], (msg: EngineMessage) => {
          if (msg.type === 'result') {
            const strats = (msg.strategies as Array<Record<string, any>>) || []
            allNames = strats.map((s: any) => String(s.name))
          }
        })
      } catch { /* empty */ }
      if (allNames.length < 2) {
        this.log(`  ${dim('Need at least 2 registered strategies. Create one: rift new my-strategy')}`)
        return this.mainMenu()
      }
      strategies = allNames.join(',')
    } else {
      const picked = await this.pickStrategy('Pick strategies to compare', true)
      strategies = picked || ''
    }
    if (!strategies) return this.mainMenu()

    const pair = (await ask(`  ${cyan('Ticker')} ${dim('(BTC)')}: `) || 'BTC').replace('-PERP', '').replace('-perp', '').toUpperCase()

    this.log('')
    await this.config.runCommand('compare', [strategies, '--pair', pair])

    // Visual summary
    this.log(`  ${bold('Recommendation:')}`)
    this.log(`    Run ${cyan('rift research <strategy> --pair <COIN>')} to validate any strategy`)
    this.log(`    Run ${cyan('rift scan --pair <COIN>')} to discover predictive features`)
    this.log('')
    this.log(`  ${bold('What next?')}`)
    this.log(`    ${cyan('1')}  Run full research on one of these`)
    this.log(`    ${cyan('2')}  Build a portfolio with these strategies`)
    this.log(`    ${cyan('3')}  Back to main menu`)
    this.log('')

    const next = await ask(`  ${cyan('>')} `)
    if (next === '1') return this.testMenu()
    if (next === '2') {
      this.log(`\n  ${dim('Run:')} ${cyan('rift portfolio backtest strategies/configs/portfolio_btc.yaml')}\n`)
    }
    return this.mainMenu()
  }

  // ═══════════════════════════════════════════
  //  PIPELINE — Full validation
  // ═══════════════════════════════════════════

  private async runPipeline(strategy: string, pair: string, tf?: string, equity: number = 10000): Promise<void> {
    this.log(`  ${bold('RIFT Research Pipeline')}`)
    this.log(`  ${dim('─'.repeat(50))}`)
    this.log(`  Strategy:  ${bold(strategy)}`)
    this.log(`  Pair:      ${pair}`)
    if (tf) this.log(`  Timeframe: ${tf}`)
    this.log(`  ${dim('─'.repeat(50))}`)
    this.log('')

    const engineArgs: string[] = [strategy, '--pair', pair, '--equity', String(equity)]
    if (tf) engineArgs.push('--tf', tf)

    let finalGrade = ''

    await runEngine('research', engineArgs, (msg: EngineMessage) => {
      if (msg.type === 'step') {
        this.log(`  ${dim(`Step ${msg.step}/5`)} ${msg.msg}`)
      } else if (msg.type === 'step_done') {
        this.log(`  ${green('✔')} ${msg.msg}`)
      } else if (msg.type === 'progress' && msg.msg) {
        this.log(`  ${dim(String(msg.msg))}`)
      } else if (msg.type === 'result') {
        finalGrade = msg.grade as string
        this.renderGradedResult(msg)
      } else if (msg.type === 'error') {
        this.log(`  ${red('✘')} ${msg.msg}`)
      }
    })

    // Post-pipeline guidance. When stdin isn't a TTY (README quickstart,
    // piped/scripted use, CI), printing an interactive menu and awaiting
    // input either hangs forever or — with </dev/null — drops the user
    // at an unanswerable prompt. Detect and print actionable copyable
    // commands instead, then exit cleanly.
    if (!process.stdin.isTTY) {
      this.log(`  ${bold('Next steps:')}`)
      if (finalGrade === 'A' || finalGrade === 'B') {
        this.log(`    ${cyan(`rift algo ${strategy} --pair ${pair}`)}  ${dim('— go live with this strategy')}`)
        this.log(`    ${cyan(`rift sweep ${strategy} --pair ${pair}`)}  ${dim('— optimize parameters')}`)
        this.log(`    ${cyan(`rift backtest ${strategy} --all-pairs --top 10`)}  ${dim('— test other pairs')}`)
      } else {
        this.log(`    ${cyan(`rift sweep ${strategy} --pair ${pair}`)}  ${dim('— optimize parameters (may improve the grade)')}`)
        this.log(`    ${cyan('rift strategies list')}  ${dim('— see all available strategies')}`)
        this.log(`    ${cyan(`rift research ${strategy} --pair <OTHER-COIN>`)}  ${dim('— try a different pair')}`)
      }
      this.log('')
      return
    }

    this.log(`  ${bold('What next?')}`)
    if (finalGrade === 'A' || finalGrade === 'B') {
      this.log(`    ${cyan('1')}  Go live ${dim(`→ rift algo ${strategy} --pair ${pair}`)}`)
      this.log(`    ${cyan('2')}  Optimize parameters`)
      this.log(`    ${cyan('3')}  Test on different pairs`)
      this.log(`    ${cyan('4')}  Back to Research Lab`)
    } else {
      this.log(`    ${cyan('1')}  Optimize parameters ${dim('— might improve the grade')}`)
      this.log(`    ${cyan('2')}  Try a different strategy`)
      this.log(`    ${cyan('3')}  Try a different pair`)
      this.log(`    ${cyan('4')}  Back to Research Lab`)
    }
    this.log('')

    const next = await ask(`  ${cyan('>')} `)

    if (finalGrade === 'A' || finalGrade === 'B') {
      if (next === '1') {
        this.log(`\n  ${dim('Starting algo trading...')}\n`)
        await this.config.runCommand('algo', [strategy, '--pair', pair])
        return this.mainMenu()
      } else if (next === '2') return this.optimizeMenu()
      else if (next === '3') {
        this.log('')
        await this.config.runCommand('backtest', [strategy, '--all-pairs', '--top', '10'])
        return this.mainMenu()
      } else if (next === '4') return this.mainMenu()
      else return this.mainMenu()
    } else {
      if (next === '1') return this.optimizeMenu()
      else if (next === '2') return this.testMenu()
      else if (next === '3') {
        const newPair = await ask(`  ${cyan('New ticker')}: `)
        if (newPair) {
          return this.runPipeline(strategy, newPair.replace('-PERP', '').replace('-perp', '').toUpperCase(), tf, equity)
        }
        return this.mainMenu()
      } else if (next === '4') return this.mainMenu()
      else return this.mainMenu()
    }
  }

  // ═══════════════════════════════════════════
  //  GRADE RENDER
  // ═══════════════════════════════════════════

  private renderGradedResult(msg: EngineMessage): void {
    const grade = msg.grade as string
    const verdict = msg.verdict as string
    const bt = msg.backtest as Record<string, any>
    const wf = msg.walkforward as Record<string, any>
    const mc = msg.montecarlo as Record<string, any>
    const multi = msg.multi_pair as Array<Record<string, any>>

    const iw = 53
    const row = boxRow(iw)
    const rr = resultRow(iw)

    this.log('')

    // Grade banner
    const gradeText = `GRADE: ${gradeColor(grade)}`
    this.log(boldBoxTop(iw + 2))
    this.log(`  ${bold('║')}${padEndVis(' '.repeat(Math.floor((iw - 7) / 2)) + gradeText, iw + 1)}${bold('║')}`)
    this.log(boldBoxBottom(iw + 2))
    this.log('')

    if (grade === 'A') this.log(`  ${green(verdict)}`)
    else if (grade === 'B') this.log(`  ${cyan(verdict)}`)
    else if (grade === 'C') this.log(`  ${yellow(verdict)}`)
    else this.log(`  ${red(verdict)}`)
    this.log('')

    this.log(boxTop(iw))

    // Backtest
    if (bt) {
      this.log(row(`${bold('BACKTEST')}`))
      this.log(boxDivider(iw))
      this.log(rr('Return', colorNum(bt.return_pct, '%')))
      this.log(rr('Sharpe', colorNum(bt.sharpe)))
      this.log(rr('Profit Factor', String(bt.profit_factor)))
      this.log(rr('Max Drawdown', colorNum(bt.max_drawdown_pct, '%')))
      this.log(rr('Win Rate', `${bt.win_rate}%`))
      this.log(rr('Trades', String(bt.num_trades)))
    }

    // Walk-Forward
    if (wf && !wf.error) {
      this.log(boxDivider(iw))
      this.log(row(`${bold('WALK-FORWARD')}`))
      this.log(boxDivider(iw))
      const deg = wf.degradation_ratio
      const degLabel = deg >= 0.7 ? green('ROBUST') : deg >= 0.4 ? yellow('MODERATE') : deg > 0 ? red('WEAK') : red('OVERFIT')
      this.log(rr('Degradation', `${deg} — ${degLabel}`))
      this.log(rr('Profitable Windows', `${wf.profitable_windows}%`))
      this.log(rr('Combined OOS Return', colorNum(wf.combined_oos_return, '%')))
    }

    // Monte Carlo
    if (mc && !mc.error) {
      this.log(boxDivider(iw))
      this.log(row(`${bold('MONTE CARLO')}`))
      this.log(boxDivider(iw))
      const probColor = mc.prob_profit >= 85 ? green : mc.prob_profit >= 60 ? yellow : red
      this.log(rr('Profit Probability', probColor(`${mc.prob_profit}%`)))
      this.log(rr('Ruin Probability', mc.prob_ruin === 0 ? green(`${mc.prob_ruin}%`) : red(`${mc.prob_ruin}%`)))
      this.log(rr('Worst Case (5th)', colorNum(mc.p5, '%')))
      this.log(rr('Median', colorNum(mc.p50, '%')))
    }

    // Multi-pair
    if (multi && multi.length > 0) {
      this.log(boxDivider(iw))
      this.log(row(`${bold('MULTI-PAIR TEST')}`))
      this.log(boxDivider(iw))
      for (const r of multi) {
        const marker = r.return_pct > 0 ? green('✔') : red('✘')
        this.log(row(` ${marker} ${r.pair.padEnd(10)} ${padEndVis(colorNum(r.return_pct, '%'), 14)} Sharpe ${colorNum(r.sharpe)}`))
      }
      const profitable = multi.filter(r => r.return_pct > 0).length
      this.log(row(` ${dim(`Profitable on ${profitable}/${multi.length} additional pairs`)}`))
    }

    this.log(boxBottom(iw))
    this.log('')
  }

  // ═══════════════════════════════════════════
  //  EXPLORE SUBMENUS
  // ═══════════════════════════════════════════

  // ─── 1. Indicator catalog ────────────────────

  private async indicatorCatalogMenu(category: string = ''): Promise<void> {
    type IndItem = {name: string; description: string; params: Array<{name: string; default: any}>}
    type IndResult = {
      total: number
      categories: Record<string, IndItem[]>
      uncategorized: IndItem[]
    }
    let data: IndResult | null = null
    try {
      const args = ['indicators']
      if (category) args.push('--category', category)
      await runEngine(args[0], args.slice(1), (msg: EngineMessage) => {
        if (msg.type === 'result') data = msg as any
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
      return this.exploreMenu()
    }
    if (!data) return this.exploreMenu()
    const d = data as IndResult

    this.log('')
    const title = category ? `INDICATOR CATALOG — ${category}` : 'INDICATOR CATALOG'
    this.log(`  ${bold(title)}  ${dim(`(${d.total} indicators)`)}`)
    this.log(`  ${dim('─'.repeat(60))}`)

    const renderItems = (items: IndItem[]) => {
      for (const it of items) {
        const params = it.params.map(p =>
          p.default !== null && p.default !== undefined ? `${p.name}=${p.default}` : p.name
        ).join(', ')
        const paramStr = params ? `(${params})` : ''
        this.log(`    ${cyan(it.name.padEnd(22))} ${dim(paramStr.padEnd(30))} ${dim(it.description)}`)
      }
    }

    for (const [cat, items] of Object.entries(d.categories)) {
      this.log('')
      this.log(`  ${bold(cat.toUpperCase())} ${dim(`(${items.length})`)}`)
      renderItems(items)
    }
    if (d.uncategorized.length > 0) {
      this.log('')
      this.log(`  ${yellow('UNCATEGORIZED')} ${dim(`(${d.uncategorized.length} — update _INDICATOR_CATEGORIES)`)}`)
      renderItems(d.uncategorized)
    }

    this.log('')
    if (!category) {
      this.log(`  ${dim('Filter:')} ${cyan('1')} trend  ${cyan('2')} momentum  ${cyan('3')} volatility  ${cyan('4')} volume`)
      this.log(`  ${dim('       ')} ${cyan('5')} structure  ${cyan('6')} adaptive  ${cyan('7')} cross_asset  ${cyan('8')} order_flow`)
      this.log(`  ${dim('       ')} ${cyan('s')} search by name/description    ${cyan('b')} back`)
    } else {
      this.log(`  ${cyan('a')} show all categories    ${cyan('b')} back`)
    }
    this.log('')
    const choice = await ask(`  ${cyan('>')} `)
    const catMap: Record<string, string> = {
      '1': 'trend', '2': 'momentum', '3': 'volatility', '4': 'volume',
      '5': 'structure', '6': 'adaptive', '7': 'cross_asset', '8': 'order_flow',
    }
    if (catMap[choice]) return this.indicatorCatalogMenu(catMap[choice])
    if (choice === 'a' || choice === 'A') return this.indicatorCatalogMenu('')
    if (choice === 's' || choice === 'S') return this.indicatorSearchMenu()
    return this.exploreMenu()
  }

  private async indicatorSearchMenu(): Promise<void> {
    const q = await ask(`  ${cyan('Search term')}: `)
    if (!q) return this.exploreMenu()
    try {
      await runEngine('indicators', ['--search', q], (msg: EngineMessage) => {
        if (msg.type !== 'result') return
        const d = msg as any
        this.log('')
        this.log(`  ${bold('SEARCH:')} "${q}"  ${dim(`(${d.total} matches)`)}`)
        this.log(`  ${dim('─'.repeat(60))}`)
        for (const [cat, items] of Object.entries(d.categories as Record<string, any[]>)) {
          for (const it of items) {
            const params = it.params.map((p: any) =>
              p.default !== null && p.default !== undefined ? `${p.name}=${p.default}` : p.name
            ).join(', ')
            const paramStr = params ? `(${params})` : ''
            this.log(`    ${cyan(it.name.padEnd(22))} ${dim(cat.padEnd(14))} ${dim(paramStr.padEnd(28))} ${dim(it.description)}`)
          }
        }
        this.log('')
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
    }
    await ask(`  ${dim('Press Enter to continue')} `)
    return this.exploreMenu()
  }

  // ─── 4. Signal forensics ─────────────────────

  private async signalForensicsMenu(): Promise<void> {
    this.log('')
    this.log(`  ${bold('SIGNAL FORENSICS')}`)
    this.log(`  ${dim('─'.repeat(60))}`)
    this.log('')
    this.log(`    ${cyan('1')}  Signal stats        ${dim('hit rate + edge per signal on a coin')}`)
    this.log(`    ${cyan('2')}  Signal decay        ${dim('does the signal lose edge over time?')}`)
    this.log(`    ${cyan('3')}  Signal backfill     ${dim('compute missing signal series from cache')}`)
    this.log(`    ${cyan('b')}  Back`)
    this.log('')
    const choice = await ask(`  ${cyan('>')} `)
    if (choice === 'b' || choice === 'B') return this.exploreMenu()

    const coin = (await ask(`  ${cyan('Coin')} ${dim('(BTC)')}: `) || 'BTC').toUpperCase()
    const tf = await ask(`  ${cyan('Timeframe')} ${dim('(1h)')}: `) || '1h'

    let cmd = ''
    let args: string[] = []
    if (choice === '1') { cmd = 'signal-stats'; args = ['--pair', coin, '--tf', tf] }
    else if (choice === '2') { cmd = 'signal-decay'; args = ['--pair', coin, '--tf', tf] }
    else if (choice === '3') { cmd = 'signal-backfill'; args = ['--pair', coin, '--tf', tf] }
    else return this.signalForensicsMenu()

    this.log('')
    this.log(dim(`  Running ${cmd}...`))
    try {
      await runEngine(cmd, args, (msg: EngineMessage) => {
        if (msg.type === 'result') {
          // Print structured JSON for now; per-command rendering can come later.
          this.log(JSON.stringify(msg, null, 2))
        } else if (msg.type === 'error') {
          this.log(`  ${red('✘')} ${msg.msg}`)
        }
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
    }
    this.log('')
    await ask(`  ${dim('Press Enter to continue')} `)
    return this.signalForensicsMenu()
  }

  // ─── 5. Funding rate browser ─────────────────

  private async fundingBrowserMenu(): Promise<void> {
    let data: any = null
    try {
      await runEngine('funding-browser', ['--top', '20'], (msg: EngineMessage) => {
        if (msg.type === 'result') data = msg
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
      return this.exploreMenu()
    }
    if (!data) return this.exploreMenu()
    const rows = (data.coins || []) as any[]

    this.log('')
    this.log(`  ${bold('FUNDING RATE BROWSER')}  ${dim(`(${data.lookback_days}d stats, sorted by |current rate|)`)}`)
    this.log(`  ${dim('─'.repeat(76))}`)
    if (rows.length === 0) {
      this.log('')
      this.log(`  ${yellow('!')} No funding data cached yet.`)
      this.log('')
      this.log(`  ${dim('To populate:')}`)
      this.log(`    ${cyan('rift fetch BTC --tf 1h')}            ${dim('— fetches candles + funding (free, HL info)')}`)
      this.log(`    ${cyan('rift sync --include-funding')}        ${dim('— full historical (requires AWS for HL S3)')}`)
      this.log('')
      await ask(`  ${dim('Press Enter to continue')} `)
      return this.exploreMenu()
    }
    this.log(`  ${dim('coin'.padEnd(10))} ${dim('current/hr'.padStart(12))} ${dim('mean/hr'.padStart(12))} ${dim('min'.padStart(12))} ${dim('max'.padStart(12))} ${dim('zscore'.padStart(8))}`)
    for (const r of rows) {
      const cur = (r.current_pct_per_hour * 1).toFixed(4) + '%'
      const mean = (r.mean_rate * 100).toFixed(4) + '%'
      const min = (r.min_rate * 100).toFixed(4) + '%'
      const max = (r.max_rate * 100).toFixed(4) + '%'
      const z = r.zscore.toFixed(2)
      const curColor = r.current_rate > 0 ? green : r.current_rate < 0 ? red : dim
      const zColor = Math.abs(r.zscore) >= 2 ? yellow : dim
      this.log(`  ${cyan(r.coin.padEnd(10))} ${curColor(cur.padStart(12))} ${dim(mean.padStart(12))} ${dim(min.padStart(12))} ${dim(max.padStart(12))} ${zColor(z.padStart(8))}`)
    }
    this.log('')
    this.log(dim('  positive = longs pay shorts (longs overcrowded)'))
    this.log(dim('  |z|≥2    = currently extreme vs the trailing window'))
    this.log('')
    await ask(`  ${dim('Press Enter to continue')} `)
    return this.exploreMenu()
  }

  // ─── 6. Order flow browser ───────────────────

  private async orderFlowBrowserMenu(): Promise<void> {
    let data: any = null
    try {
      await runEngine('order-flow', ['--top', '20'], (msg: EngineMessage) => {
        if (msg.type === 'result') data = msg
      })
    } catch (e: any) {
      this.log(`  ${red('✘')} ${e.message}`)
      return this.exploreMenu()
    }
    if (!data) return this.exploreMenu()
    const rows = (data.coins || []) as any[]

    this.log('')
    this.log(`  ${bold('ORDER FLOW BROWSER')}  ${dim(`(${data.lookback_hours}h, sorted by |imbalance|)`)}`)
    this.log(`  ${dim('─'.repeat(80))}`)
    if (rows.length === 0) {
      this.log('')
      this.log(`  ${yellow('!')} No fill data cached yet.`)
      this.log('')
      this.log(`  ${dim('Fill data only comes from HL\'s S3 archive (requester-pays).')}`)
      this.log(`  ${dim('To populate:')}`)
      this.log(`    ${cyan('rift sync --coins BTC --include-fills')}   ${dim('— requires AWS credentials')}`)
      this.log('')
      this.log(`  ${dim('No free path exists for order-flow data (HL info endpoint')}`)
      this.log(`  ${dim('doesn\'t expose per-fill data, only aggregated candles).')}`)
      this.log('')
      await ask(`  ${dim('Press Enter to continue')} `)
      return this.exploreMenu()
    }
    this.log(`  ${dim('coin'.padEnd(8))} ${dim('fills'.padStart(8))} ${dim('imbalance'.padStart(12))} ${dim('taker'.padStart(8))} ${dim('opens'.padStart(12))} ${dim('closes'.padStart(12))} ${dim('net flow'.padStart(12))}`)
    for (const r of rows) {
      const imb = (r.imbalance_pct).toFixed(2) + '%'
      const taker = isNaN(r.taker_ratio) ? '—' : (r.taker_ratio * 100).toFixed(1) + '%'
      const opens = isNaN(r.opens) ? '—' : r.opens.toFixed(0)
      const closes = isNaN(r.closes) ? '—' : r.closes.toFixed(0)
      const netflow = isNaN(r.net_flow) ? '—' : r.net_flow.toFixed(0)
      const imbColor = r.imbalance > 0.02 ? green : r.imbalance < -0.02 ? red : dim
      this.log(`  ${cyan(r.coin.padEnd(8))} ${dim(String(r.fills).padStart(8))} ${imbColor(imb.padStart(12))} ${dim(taker.padStart(8))} ${dim(opens.padStart(12))} ${dim(closes.padStart(12))} ${dim(netflow.padStart(12))}`)
    }
    this.log('')
    this.log(dim('  imbalance > 0 = more buy-aggressor volume; < 0 = more sell-aggressor'))
    this.log(dim('  taker = % of fills that crossed the spread (aggressive)'))
    this.log(dim('  net flow > 0 = positions being opened; < 0 = being closed'))
    this.log('')
    await ask(`  ${dim('Press Enter to continue')} `)
    return this.exploreMenu()
  }

  // ─── 7. Cross-asset relationships ────────────

  private async crossAssetMenu(): Promise<void> {
    // Default coin list = whatever's actually cached at 1h (no hardcoded
    // RIFT-team assumptions). OSS users with only BTC cached will see
    // BTC and a helpful hint to fetch more.
    let defaultCoins = 'BTC'
    try {
      const fs = await import('node:fs')
      const path = await import('node:path')
      const dataDir = path.join(process.env.HOME || '~', '.rift', 'data')
      if (fs.existsSync(dataDir)) {
        const coins: string[] = []
        for (const entry of fs.readdirSync(dataDir)) {
          if (entry.startsWith('_')) continue
          const candleFile = path.join(dataDir, entry, '1h', 'candles.parquet')
          if (fs.existsSync(candleFile)) coins.push(entry)
        }
        if (coins.length > 0) defaultCoins = coins.slice(0, 8).join(',')
      }
    } catch { /* fall back to BTC */ }

    if (defaultCoins === 'BTC') {
      this.log('')
      this.log(`  ${dim('Only BTC cached at 1h. For a meaningful cross-asset matrix, fetch a few coins first:')}`)
      this.log(`    ${cyan('rift fetch ETH --tf 1h')}`)
      this.log(`    ${cyan('rift fetch SOL --tf 1h')}`)
      this.log(`    ${cyan('rift fetch HYPE --tf 1h')}`)
    }
    const coinsInput = await ask(`  ${cyan('Coins')} ${dim(`(${defaultCoins})`)}: `)
    const coins = coinsInput || defaultCoins
    const lookbackInput = await ask(`  ${cyan('Lookback candles')} ${dim('(720 = 30d of 1h)')}: `)
    const lookback = lookbackInput || '720'

    this.log('')
    this.log(dim('  Computing correlation + lead-lag + beta...'))
    let data: any = null
    let errMsg = ''
    try {
      await runEngine('cross-asset', ['--coins', coins, '--lookback', lookback], (msg: EngineMessage) => {
        if (msg.type === 'result') data = msg
        else if (msg.type === 'error') errMsg = String(msg.msg)
      })
    } catch (e: any) {
      errMsg = e.message
    }
    if (!data) {
      this.log('')
      this.log(`  ${yellow('!')} ${errMsg || 'No data returned.'}`)
      this.log('')
      this.log(`  ${dim('Fetch candle data first:')}`)
      this.log(`    ${cyan('rift fetch <COIN> --tf 1h')}            ${dim('— for each coin you want in the matrix')}`)
      this.log('')
      await ask(`  ${dim('Press Enter to continue')} `)
      return this.exploreMenu()
    }

    this.log('')
    this.log(`  ${bold('CROSS-ASSET MATRIX')}  ${dim(`(${data.lookback_candles} ${data.tf} candles, benchmark=${data.benchmark})`)}`)
    this.log(`  ${dim('─'.repeat(70))}`)

    const available = data.available_coins as string[]
    const skipped = data.skipped as any[]

    // Correlation matrix
    this.log('')
    this.log(`  ${bold('Correlation matrix')} ${dim('(log returns)')}`)
    const header = '       ' + available.map(c => c.padStart(8)).join('')
    this.log(`  ${dim(header)}`)
    for (const ci of available) {
      const row = available.map(cj => {
        const v = data.corr[ci][cj] as number
        const s = v.toFixed(2).padStart(8)
        if (ci === cj) return dim(s)
        if (Math.abs(v) >= 0.7) return green(s)
        if (Math.abs(v) >= 0.4) return cyan(s)
        if (Math.abs(v) >= 0.2) return dim(s)
        return dim(s)
      }).join('')
      this.log(`  ${cyan(ci.padEnd(7))}${row}`)
    }

    // Lead-lag
    this.log('')
    this.log(`  ${bold('Lead-lag vs')} ${cyan(data.benchmark)} ${dim('(positive lag = benchmark leads)')}`)
    for (const ll of data.lead_lag as any[]) {
      const lagStr = ll.best_lag > 0 ? `+${ll.best_lag}` : String(ll.best_lag)
      const corrColor = Math.abs(ll.best_corr) >= 0.5 ? green : Math.abs(ll.best_corr) >= 0.3 ? cyan : dim
      this.log(`    ${cyan(ll.coin.padEnd(8))} best lag=${lagStr.padStart(3)}  corr=${corrColor(ll.best_corr.toFixed(3))}`)
    }

    // Beta
    this.log('')
    this.log(`  ${bold('Beta vs')} ${cyan(data.benchmark)} ${dim('(>1 = more volatile, <1 = less)')}`)
    for (const b of data.beta as any[]) {
      const betaColor = b.beta > 1.5 ? red : b.beta > 1 ? yellow : b.beta < 0.5 ? cyan : dim
      this.log(`    ${cyan(b.coin.padEnd(8))} β = ${betaColor(b.beta.toFixed(3))}`)
    }

    if (skipped.length > 0) {
      this.log('')
      this.log(dim('  Skipped:'))
      for (const s of skipped) {
        this.log(`    ${dim(s.coin)}: ${dim(s.reason)}`)
      }
    }

    this.log('')
    await ask(`  ${dim('Press Enter to continue')} `)
    return this.exploreMenu()
  }

  // ─── 8. Regime browser ───────────────────────

  private async regimeBrowserMenu(): Promise<void> {
    const coin = (await ask(`  ${cyan('Coin')} ${dim('(BTC)')}: `) || 'BTC').toUpperCase()
    const tf = await ask(`  ${cyan('Timeframe')} ${dim('(1h)')}: `) || '1h'

    this.log('')
    this.log(dim(`  Classifying regime for ${coin} ${tf}...`))
    let data: any = null
    let errMsg = ''
    try {
      await runEngine('regime', ['--coin', coin, '--tf', tf], (msg: EngineMessage) => {
        if (msg.type === 'result') data = msg
        else if (msg.type === 'error') errMsg = String(msg.msg)
      })
    } catch (e: any) {
      errMsg = e.message
    }
    if (!data) {
      this.log('')
      this.log(`  ${yellow('!')} ${errMsg || `No ${coin} ${tf} data cached.`}`)
      this.log('')
      this.log(`  ${dim('Fetch it:')} ${cyan(`rift fetch ${coin} --tf ${tf}`)}`)
      this.log('')
      await ask(`  ${dim('Press Enter to continue')} `)
      return this.exploreMenu()
    }

    const cur = data.current
    const volColor = cur.vol_regime === 'high' ? red : cur.vol_regime === 'low' ? cyan : dim
    const trendColor = cur.trend_regime === 'bull' ? green : cur.trend_regime === 'bear' ? red : dim

    this.log('')
    this.log(`  ${bold('REGIME BROWSER')}  ${dim(`(${data.coin} ${data.tf}, ${data.candles_analyzed} candles)`)}`)
    this.log(`  ${dim('─'.repeat(60))}`)
    this.log('')
    this.log(`  ${bold('Right now:')}`)
    this.log(`    Vol regime:    ${volColor(cur.vol_regime.toUpperCase())}      ${dim(`ATR ${cur.atr.toFixed(2)}`)}`)
    this.log(`    Trend regime:  ${trendColor(cur.trend_regime.toUpperCase())}      ${dim(`ADX ${cur.adx.toFixed(1)}  +DI ${cur.plus_di.toFixed(1)}  -DI ${cur.minus_di.toFixed(1)}`)}`)
    this.log(`    Last close:    ${bold('$' + cur.close.toLocaleString())}`)

    this.log('')
    this.log(`  ${bold('Historical breakdown')} ${dim(`(% of analyzed candles)`)}`)
    this.log(`    Vol:`)
    const vb = data.vol_breakdown_pct as Record<string, number>
    for (const [k, v] of Object.entries(vb)) {
      const c = k === 'high' ? red : k === 'low' ? cyan : dim
      const barLen = Math.round(v / 2)
      this.log(`      ${c(k.padEnd(8))} ${c('█'.repeat(barLen))} ${dim(v.toFixed(1) + '%')}`)
    }
    this.log(`    Trend:`)
    const tb = data.trend_breakdown_pct as Record<string, number>
    for (const [k, v] of Object.entries(tb)) {
      const c = k === 'bull' ? green : k === 'bear' ? red : dim
      const barLen = Math.round(v / 2)
      this.log(`      ${c(k.padEnd(8))} ${c('█'.repeat(barLen))} ${dim(v.toFixed(1) + '%')}`)
    }
    this.log('')
    await ask(`  ${dim('Press Enter to continue')} `)
    return this.exploreMenu()
  }
}
