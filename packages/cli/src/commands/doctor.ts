import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import {hasCredentials, loadCredentials, maskKey} from '../lib/credentials.js'
import {hasApprovedFees, hasOnChainApproval, getFeeStatus, BUILDER_FEE_DISPLAY} from '../lib/fees.js'
import type {EngineMessage} from '../lib/python-bridge.js'

const ok = (s: string) => `\x1b[32m✔\x1b[0m ${s}`
const fail = (s: string) => `\x1b[31m✘\x1b[0m ${s}`
const warn = (s: string) => `\x1b[33m!\x1b[0m ${s}`
const info = (s: string) => `\x1b[36m◦\x1b[0m ${s}`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`

export default class Doctor extends GatedCommand {
  static override description = 'Check system health and diagnose issues'

  static override examples = [
    '$ rift doctor',
  ]

  async run(): Promise<void> {
    this.log('')
    this.log(`  ${bold('RIFT Doctor')}`)
    this.log(`  ${dim('─'.repeat(40))}`)
    this.log('')

    // Node.js check
    this.log(`  ${ok(`Node.js ${process.version}`)}`)

    // Builder fee check (two layers)
    if (hasApprovedFees()) {
      const status = getFeeStatus()
      this.log(`  ${ok(`Builder fee consent ${dim(status?.approvedAt ? `(${status.approvedAt.slice(0, 10)})` : '')}`)}`)
      // The "on-chain approved" status lives in two places:
      //   - ~/.rift/config.json.fees.onChainApproved (legacy TS-flow flag)
      //   - ~/.rift/credentials.builder_fee_approved (canonical, set by
      //     agent-pair + standalone approve-builder-fee)
      // Treat either as evidence the approval went through.
      const creds = loadCredentials()
      const onChainOk = hasOnChainApproval() || (creds?.builder_fee_approved === true)
      if (onChainOk) {
        this.log(`  ${ok(`On-chain approval ${dim('(ready for live trading)')}`)}`)
      } else {
        this.log(`  ${info(`On-chain approval pending ${dim('(needed for live trading only)')}`)}`)
      }
    } else {
      this.log(`  ${fail(`Builder fee not approved ${dim('— run: rift auth setup')}`)}`)
    }

    // Credentials check
    if (hasCredentials()) {
      const creds = loadCredentials()
      if (creds) {
        // `type` is optional — Python's agent-pair flow doesn't set it.
        // Omit the parenthetical when unknown rather than printing "undefined".
        const detail = creds.type
          ? `(${creds.network}, ${creds.type})`
          : `(${creds.network})`
        this.log(`  ${ok(`Wallet configured ${dim(detail)}`)}`)
      }
    } else {
      this.log(`  ${warn(`No wallet configured ${dim('— run: rift auth setup')}`)}`)
    }

    // Engine checks (Python side)
    try {
      await runEngine('doctor', [], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          const checks = msg.checks as Array<{name: string; status: string; detail: string}>
          for (const check of checks) {
            const detail = dim(check.detail)
            if (check.status === 'ok') {
              this.log(`  ${ok(`${check.name} ${detail}`)}`)
            } else if (check.status === 'warn') {
              this.log(`  ${warn(`${check.name} ${detail}`)}`)
            } else if (check.status === 'info') {
              this.log(`  ${info(`${check.name} ${detail}`)}`)
            } else {
              this.log(`  ${fail(`${check.name} ${detail}`)}`)
            }
          }
        }
      })
    } catch (error: any) {
      this.log(`  ${fail(`Python engine — ${error.message.split('\n')[0]}`)}`)
    }

    this.log('')
  }
}
