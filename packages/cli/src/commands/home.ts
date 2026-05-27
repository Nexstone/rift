import {GatedCommand} from '../lib/base-command.js'
import {
  green, cyan, bold, dim,
  ask,
} from '../lib/tui.js'

// Gradient colors — bright cyan at top, fading to dark blue at bottom
const LOGO_GRADIENT = [
  '\x1b[1m\x1b[96m',  // bright cyan (line 1)
  '\x1b[1m\x1b[36m',  // cyan (line 2)
  '\x1b[36m',          // cyan normal (line 3)
  '\x1b[34m',          // blue (line 4)
  '\x1b[2m\x1b[34m',  // dim blue (line 5)
  '\x1b[2m\x1b[34m',  // dim blue (line 6)
]

const LOGO = [
  '██████╗ ██╗███████╗████████╗',
  '██╔══██╗██║██╔════╝╚══██╔══╝',
  '██████╔╝██║█████╗     ██║   ',
  '██╔══██╗██║██╔══╝     ██║   ',
  '██║  ██║██║██║        ██║   ',
  '╚═╝  ╚═╝╚═╝╚═╝        ╚═╝   ',
]

export default class Home extends GatedCommand {
  static override description = 'RIFT — Research · Iteration · Forecast · Trade'

  static override args = {}
  static override flags = {}

  // Home renders its own bottom-of-screen status via renderStatusFooter()
  // inline — skip the auto-footer that GatedCommand.finally() adds.
  static override skipFooter = true

  async run(): Promise<void> {
    // Main loop — after each action the user returns to the menu.
    // Exit via `0`, `q`, `quit`, `exit`, or Ctrl-C.
    while (true) {
      // Logo with vertical gradient
      this.log('')
      for (let i = 0; i < LOGO.length; i++) {
        this.log(`  ${LOGO_GRADIENT[i]}${LOGO[i]}\x1b[0m`)
      }
      this.log('')
      this.log(`  ${dim('Research · Iteration · Forecast · Trade')}`)
      this.log(`  ${dim('─'.repeat(42))}`)
      this.log('')

      // Menu
      this.log(`    ${cyan('1')}  ${bold('Scout')}               ${dim('scan the market for opportunities')}`)
      this.log(`    ${cyan('2')}  ${bold('Trade')}               ${dim('manual trade with live monitoring')}`)
      this.log(`    ${cyan('3')}  ${bold('Research Lab')}        ${dim('discover, build, test, optimize')}`)
      this.log(`    ${cyan('4')}  ${bold('Algo Trading')}        ${dim('automated strategy trading')}`)
      this.log(`    ${cyan('5')}  ${bold('Portfolio Manager')}   ${dim('multi-strategy algo trading')}`)
      this.log('')
      this.log(`    ${cyan('6')}  ${dim('Doctor')}              ${dim('system health check')}`)
      this.log(`    ${cyan('7')}  ${dim('Settings')}            ${dim('wallet, config, proxy')}`)
      this.log(`    ${cyan('8')}  ${dim('AI Integration')}      ${dim('connect Claude, Cursor, or any AI')}`)
      this.log('')
      this.log(`    ${cyan('0')}  ${dim('Exit')}`)
      this.log('')
      this.log(`    ${dim('Tip:')} ${cyan('rift more')} ${dim('shows every engine command')}`)
      this.log('')

      // Phase 0 status footer — reflects real on-disk state
      const {renderStatusFooter} = await import('../lib/status-footer.js')
      this.log(renderStatusFooter())
      this.log('')

      // Input
      const choice = await ask(`  ${cyan('>')} `)

      switch (choice) {
        case '0':
        case 'q':
        case 'quit':
        case 'exit':
          this.log('')
          return
        case '1':
          await this.dispatch('scout')
          break
        case '2':
          await this.dispatch('trade')
          break
        case '3':
          await this.dispatch('research')
          break
        case '4':
          await this.dispatch('algo')
          break
        case '5':
          await this.dispatch('portfolio:status')
          break
        case '6':
          await this.dispatch('doctor')
          break
        case '7':
          await this.settingsMenu()
          break
        case '8':
          await this.aiIntegrationMenu()
          break
        default:
          if (choice) {
            this.log(`\n  ${dim('Unknown option. Try 0-8.')}\n`)
          }
      }
    }
  }

  /**
   * Run a subcommand and prevent its output from being clobbered by the
   * next menu redraw.
   *
   * If the command exits in < 1500ms (almost always a precondition failure
   * like "wallet not configured" that prints an error and bails), pause for
   * keypress so the user can actually read what happened before the menu
   * redraws over it.
   *
   * If the command ran for longer, the user was interacting with it and
   * doesn't need a pause — redraw immediately for a clean menu transition.
   */
  private async dispatch(cmd: string, args: string[] = []): Promise<void> {
    const start = Date.now()
    try {
      await this.config.runCommand(cmd, args)
    } catch (err: any) {
      // Surface unexpected throws so the user can see them.
      this.log(`\n  ${dim('Command threw:')} ${err?.message ?? err}\n`)
    }
    const elapsed = Date.now() - start
    if (elapsed < 1500) {
      await ask(`  ${dim('Press Enter to return to menu...')} `)
    }
  }

  private async aiIntegrationMenu(): Promise<void> {
    this.log('')
    this.log(`  ${bold('AI Integration')} ${dim('— connect AI agents to RIFT')}`)
    this.log(`  ${dim('─'.repeat(50))}`)
    this.log('')
    this.log(`  RIFT exposes ${bold('59 tools')} via MCP (Model Context Protocol).`)
    this.log(`  Any AI agent can research, backtest, optimize, and`)
    this.log(`  build strategies through RIFT autonomously.`)
    this.log('')
    this.log(`  ${bold('Claude Desktop / Claude Code:')}`)
    this.log('')
    this.log(`  Add to your config file:`)
    this.log('')
    this.log(`  ${dim('{')}`)
    this.log(`    ${dim('"mcpServers":')} ${dim('{')}`)
    this.log(`      ${dim('"rift":')} ${dim('{')}`)
    this.log(`        ${cyan('"command"')}: ${green('"rift"')},`)
    this.log(`        ${cyan('"args"')}: [${green('"serve"')}]`)
    this.log(`      ${dim('}')}`)
    this.log(`    ${dim('}')}`)
    this.log(`  ${dim('}')}`)
    this.log('')
    this.log(`  ${dim('Config location:')}`)
    this.log(`    ${dim('macOS:')} ~/Library/Application Support/Claude/claude_desktop_config.json`)
    this.log(`    ${dim('Windows:')} %APPDATA%/Claude/claude_desktop_config.json`)
    this.log('')
    this.log(`  ${bold('Tool categories:')}`)
    this.log(`    ${dim('Research      backtest, research, compare, sweep, smart_sweep,')}`)
    this.log(`    ${dim('              walk_forward, montecarlo, quick_test, verify,')}`)
    this.log(`    ${dim('              indicator_stats, feature_importance, tearsheet')}`)
    this.log(`    ${dim('Trade         scout, scan, manual_trade, buy, sell,')}`)
    this.log(`    ${dim('              close_position, reduce_position, tighten_stop')}`)
    this.log(`    ${dim('Algo          algo_start, algo_status, algo_stop')}`)
    this.log(`    ${dim('Portfolio     portfolio_start, portfolio_status,')}`)
    this.log(`    ${dim('              portfolio_stop, portfolio_alerts')}`)
    this.log(`    ${dim('Account       balance, holdings, state, transfer,')}`)
    this.log(`    ${dim('              deposit, withdraw, auth_setup, auth_status')}`)
    this.log(`    ${dim('Reports       tca_report, pnl_attribution, var_report,')}`)
    this.log(`    ${dim('              generate_report, audit_export, history')}`)
    this.log(`    ${dim('Data          fetch_data, list_data, data_inventory')}`)
    this.log(`    ${dim('Workbench     workbench_create, workbench_update,')}`)
    this.log(`    ${dim('              workbench_show, save_optimized, strategy_versions')}`)
    this.log(`    ${dim('System        doctor, health, cost, lessons, add_lesson,')}`)
    this.log(`    ${dim('              guide, list_strategies, experiments,')}`)
    this.log(`    ${dim('              api_start, watchdog_events')}`)
    this.log('')
    this.log(`  ${dim('After adding the config, restart Claude Desktop.')}`)
    this.log(`  ${dim('Claude will automatically start RIFT when it needs trading tools.')}`)
    this.log('')

    await ask(`  ${dim('Press Enter to go back')} `)
    // Parent run() loop redraws the main menu.
  }

  private async settingsMenu(): Promise<void> {
    while (true) {
      this.log('')
      this.log(`  ${bold('Settings')}`)
      this.log(`  ${dim('─'.repeat(30))}`)
      this.log('')
      this.log(`    ${cyan('1')}  ${bold('Auth')}     ${dim('wallet setup for live trading')}`)
      this.log(`    ${cyan('2')}  ${bold('Config')}   ${dim('view/edit configuration')}`)
      this.log(`    ${cyan('3')}  ${bold('Proxy')}    ${dim('network proxy setup')}`)
      this.log(`    ${cyan('4')}  ${dim('Back')}`)
      this.log('')

      const choice = await ask(`  ${cyan('>')} `)

      switch (choice) {
        case '1':
          await this.dispatch('auth', ['setup'])
          break
        case '2':
          await this.dispatch('config', ['list'])
          break
        case '3':
          this.log(`\n  ${dim('Run:')} ${cyan('rift setup proxy')}\n`)
          break
        case '4':
        case '0':
        case 'q':
          return
        default:
          if (choice) {
            this.log(`\n  ${dim('Unknown option. Try 1-4.')}\n`)
          }
      }
    }
  }
}
