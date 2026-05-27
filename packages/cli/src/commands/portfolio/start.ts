import {Flags} from '@oclif/core'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {GatedCommand} from '../../lib/base-command.js'
import {spawnDaemon, getDataDir} from '../../lib/python-bridge.js'
import {loadCredentials, hasFullSetup, getAccountAddress} from '../../lib/credentials.js'
import {
  green, red, cyan, bold, dim, greenBg,
} from '../../lib/tui.js'

export default class PortfolioStart extends GatedCommand {
  static override description = 'Start the portfolio supervisor to manage multiple live strategies'

  static override examples = [
    '$ rift portfolio start',
    '$ rift portfolio start --config ~/my-portfolio.yaml',
  ]

  static override flags = {
    config: Flags.string({description: 'Path to portfolio.yaml', default: ''}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(PortfolioStart)

    if (!hasFullSetup()) {
      this.log(`\n  ${red('✘')} Portfolio requires account setup. Run: ${cyan('rift auth setup')}\n`)
      return
    }

    const creds = loadCredentials()
    if (!creds) {
      this.log(`\n  ${red('✘')} No credentials found.\n`)
      return
    }

    // Check if supervisor already running
    const pidFile = path.join(getDataDir(), 'algo', 'supervisor.pid')
    if (fs.existsSync(pidFile)) {
      try {
        const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
        process.kill(pid, 0)
        this.log(`\n  ${greenBg(' ● PORTFOLIO ')} Supervisor already running ${dim(`(PID ${pid})`)}\n`)
        this.log(`  ${dim(`Run ${cyan('rift portfolio status')} to view or ${cyan('rift portfolio stop')} to end.`)}\n`)
        return
      } catch {
        fs.unlinkSync(pidFile)
      }
    }

    // Check portfolio config exists
    const configPath = flags.config || path.join(getDataDir(), 'algo', 'portfolio.yaml')
    if (!fs.existsSync(configPath)) {
      this.log('')
      this.log(`  ${red('✘')} No portfolio config found at: ${dim(configPath)}`)
      this.log(`  ${dim(`Run ${cyan('rift portfolio create')} to build one.`)}`)
      this.log('')
      return
    }

    // Spawn supervisor daemon
    const {pid} = spawnDaemon('portfolio-start', [
      '--config', configPath,
      '--account', getAccountAddress(creds),
      
    ], {
      HYPERLIQUID_PRIVATE_KEY: creds.private_key,
    })

    this.log('')
    this.log(`  ${greenBg(' ● PORTFOLIO ')} Supervisor started ${dim(`(PID ${pid})`)}`)
    this.log(`  ${dim('Managing strategies in background.')}`)
    this.log(`  ${dim(`Run ${cyan('rift portfolio status')} to monitor.`)}`)
    this.log('')
  }
}
