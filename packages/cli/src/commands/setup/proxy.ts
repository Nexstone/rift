import {GatedCommand} from '../../lib/base-command.js'
import {createInterface} from 'node:readline'
import {execSync} from 'node:child_process'
import {runEngine} from '../../lib/python-bridge.js'
import type {EngineMessage} from '../../lib/python-bridge.js'

const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const yellow = (s: string) => `\x1b[33m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

function ask(question: string): Promise<string> {
  const rl = createInterface({input: process.stdin, output: process.stdout})
  return new Promise(resolve => {
    rl.question(question, answer => {
      rl.close()
      resolve(answer.trim())
    })
  })
}

async function checkApi(proxy?: string): Promise<{ok: boolean; latency?: number; pairs?: number; error?: string}> {
  return new Promise(resolve => {
    const args = proxy ? ['--proxy', proxy] : []
    let result: any = null

    runEngine('check-api', args, (msg: EngineMessage) => {
      if (msg.type === 'result') {
        result = msg
      }
    })
      .then(() => {
        if (result?.status === 'ok') {
          resolve({ok: true, latency: result.latency_ms, pairs: result.pairs})
        } else {
          resolve({ok: false, error: result?.error || 'Unknown error'})
        }
      })
      .catch((err: Error) => {
        resolve({ok: false, error: err.message.split('\n')[0]})
      })
  })
}

async function saveProxy(proxyUrl: string): Promise<void> {
  return new Promise((resolve, reject) => {
    runEngine('set-proxy', [proxyUrl], () => {})
      .then(resolve)
      .catch(reject)
  })
}

async function clearProxy(): Promise<void> {
  return new Promise((resolve, reject) => {
    runEngine('clear-proxy', [], () => {})
      .then(resolve)
      .catch(reject)
  })
}

function detectVpnClients(): Array<{name: string; detected: boolean; hint: string}> {
  const clients = [
    {
      name: 'WireGuard',
      cmd: 'which wg',
      hint: 'WireGuard detected. Make sure your tunnel is active: wg show',
    },
    {
      name: 'Tailscale',
      cmd: 'which tailscale',
      hint: 'Tailscale detected. Use an exit node in a supported region: tailscale up --exit-node=<node>',
    },
    {
      name: 'Mullvad',
      cmd: 'which mullvad',
      hint: 'Mullvad detected. Connect to a non-US server: mullvad connect',
    },
    {
      name: 'NordVPN',
      cmd: 'which nordvpn',
      hint: 'NordVPN detected. Connect to a non-US server: nordvpn connect',
    },
    {
      name: 'ProtonVPN',
      cmd: 'which protonvpn-cli',
      hint: 'ProtonVPN detected. Connect to a non-US server: protonvpn-cli connect',
    },
    {
      name: 'OpenVPN',
      cmd: 'which openvpn',
      hint: 'OpenVPN detected. Connect with your config: sudo openvpn --config your.ovpn',
    },
  ]

  return clients.map(c => {
    let detected = false
    try {
      execSync(c.cmd, {stdio: 'ignore'})
      detected = true
    } catch {}
    return {name: c.name, detected, hint: c.hint}
  })
}

export default class SetupProxy extends GatedCommand {
  static override description = 'Set up proxy for Hyperliquid API access'

  static override examples = [
    '$ rift setup proxy',
  ]

  async run(): Promise<void> {
    this.log('')
    this.log(`  ${bold('RIFT Proxy Setup')}`)
    this.log(`  ${dim('─'.repeat(45))}`)
    this.log('')

    // Step 1: Check direct connection
    this.log(`  ${dim('Checking Hyperliquid API access...')}`)
    const direct = await checkApi()

    if (direct.ok) {
      this.log(`  ${green('✔')} Connected directly ${dim(`(${direct.latency}ms, ${direct.pairs} pairs)`)}`)
      this.log('')
      this.log(`  ${dim('No proxy needed — your connection works.')}`)

      const keepGoing = await ask(`\n  ${dim('Set up a proxy anyway?')} ${dim('(y/N)')}: `)
      if (keepGoing.toLowerCase() !== 'y') {
        this.log('')
        return
      }
    } else {
      this.log(`  ${red('✘')} Connection blocked`)
      this.log(`    ${dim(direct.error || 'Could not reach Hyperliquid API')}`)
      this.log('')
      this.log(`  ${dim("This is likely a geographic restriction. Let's fix it.")}`)
    }

    this.log('')

    // Step 2: Options
    this.log(`  ${bold('How would you like to connect?')}`)
    this.log('')
    this.log(`    ${cyan('1')}  I already have a VPN running        ${dim('— just test the connection')}`)
    this.log(`    ${cyan('2')}  I have a proxy/SOCKS5 address        ${dim('— paste it in')}`)
    this.log(`    ${cyan('3')}  Help me set one up                   ${dim("— I'll walk you through it")}`)
    this.log(`    ${cyan('4')}  Remove existing proxy config         ${dim('— go back to direct connection')}`)
    this.log('')

    const choice = await ask(`  ${cyan('>')} `)

    switch (choice) {
      case '1':
        await this.optionVpnRunning()
        break
      case '2':
        await this.optionPasteProxy()
        break
      case '3':
        await this.optionGuidedSetup()
        break
      case '4':
        await this.optionClearProxy()
        break
      default:
        this.log(dim('  Invalid selection.'))
    }

    this.log('')
  }

  private async optionVpnRunning(): Promise<void> {
    this.log('')
    this.log(`  ${dim('Testing connection with your VPN...')}`)

    const result = await checkApi()

    if (result.ok) {
      this.log(`  ${green('✔')} Connected! ${dim(`(${result.latency}ms, ${result.pairs} pairs)`)}`)
      this.log('')
      this.log(`  ${dim('Your VPN is routing traffic correctly. No proxy config needed.')}`)
      this.log(`  ${dim('Just keep your VPN on when using RIFT.')}`)
    } else {
      this.log(`  ${red('✘')} Still blocked. ${dim(result.error || '')}`)
      this.log('')
      this.log(`  ${dim('Your VPN may not be routing all traffic, or the exit node')}`)
      this.log(`  ${dim('is in a restricted region. Try a different server location,')}`)
      this.log(`  ${dim('or use option 2 to configure a SOCKS5 proxy.')}`)
    }
  }

  private async optionPasteProxy(): Promise<void> {
    this.log('')
    this.log(`  ${dim('Paste your proxy URL. Common formats:')}`)
    this.log(`    ${dim('socks5://127.0.0.1:1080')}`)
    this.log(`    ${dim('socks5://user:pass@host:port')}`)
    this.log(`    ${dim('http://host:port')}`)
    this.log(`    ${dim('https://host:port')}`)
    this.log('')

    const proxy = await ask(`  ${cyan('Proxy URL')}: `)
    if (!proxy) {
      this.log(dim('  No proxy entered.'))
      return
    }

    this.log('')
    this.log(`  ${dim('Testing connection via')} ${proxy} ${dim('...')}`)

    const result = await checkApi(proxy)

    if (result.ok) {
      this.log(`  ${green('✔')} Connected! ${dim(`(${result.latency}ms, ${result.pairs} pairs)`)}`)
      this.log('')

      await saveProxy(proxy)

      this.log(`  ${green('✔')} Proxy saved to ${dim('~/.rift/config.json')}`)
      this.log(`  ${dim('All RIFT API calls will now use this proxy.')}`)
    } else {
      this.log(`  ${red('✘')} Failed to connect via proxy`)
      this.log(`    ${dim(result.error || 'Unknown error')}`)
      this.log('')
      this.log(`  ${dim('Check that the proxy is running and the URL is correct.')}`)

      const saveAnyway = await ask(`\n  ${dim('Save this proxy anyway?')} ${dim('(y/N)')}: `)
      if (saveAnyway.toLowerCase() === 'y') {
        await saveProxy(proxy)
        this.log(`  ${yellow('!')} Proxy saved (untested)`)
      }
    }
  }

  private async optionGuidedSetup(): Promise<void> {
    this.log('')

    // Detect installed VPN clients
    const clients = detectVpnClients()
    const installed = clients.filter(c => c.detected)

    if (installed.length > 0) {
      this.log(`  ${bold('Detected VPN clients on your system:')}`)
      this.log('')
      for (const client of installed) {
        this.log(`  ${green('✔')} ${bold(client.name)}`)
        this.log(`    ${dim(client.hint)}`)
        this.log('')
      }

      this.log(`  ${dim('Connect using one of the above, then re-run:')}`)
      this.log(`    ${cyan('rift setup proxy')}`)
      this.log(`  ${dim('and select option 1 to test your connection.')}`)
      this.log('')
      this.log(`  ${dim('─'.repeat(45))}`)
      this.log('')
    }

    // SOCKS5 provider guides
    this.log(`  ${bold('Quick setup options:')}`)
    this.log('')

    this.log(`  ${cyan('A')}  ${bold('Mullvad SOCKS5')} ${dim('— easiest, $5/mo, no account needed')}`)
    this.log(`     ${dim('1. Go to mullvad.net and get an account number')}`)
    this.log(`     ${dim('2. Download & install Mullvad')}`)
    this.log(`     ${dim('3. Connect to any non-US server')}`)
    this.log(`     ${dim('4. SOCKS5 proxy is at: socks5://10.64.0.1:1080')}`)
    this.log(`     ${dim('5. Come back and run: rift setup proxy → option 2')}`)
    this.log('')

    this.log(`  ${cyan('B')}  ${bold('SSH tunnel')} ${dim('— if you have a VPS ($5/mo)')}`)
    this.log(`     ${dim('Run this in a separate terminal:')}`)
    this.log(`     ${cyan('ssh -D 1080 -N user@your-server-ip')}`)
    this.log(`     ${dim('Then come back and enter: socks5://127.0.0.1:1080')}`)
    this.log('')

    this.log(`  ${cyan('C')}  ${bold('NordVPN SOCKS5')} ${dim('— if you have NordVPN')}`)
    this.log(`     ${dim('1. Get SOCKS5 credentials from nordvpn.com/servers/socks')}`)
    this.log(`     ${dim('2. Use: socks5://user:pass@amsterdam.nl.socks.nordhold.net:1080')}`)
    this.log('')

    this.log(`  ${cyan('D')}  ${bold('ProtonVPN SOCKS5')} ${dim('— if you have ProtonVPN Plus')}`)
    this.log(`     ${dim('1. Enable SOCKS5 in ProtonVPN settings')}`)
    this.log(`     ${dim('2. Use: socks5://127.0.0.1:1080')}`)
    this.log('')

    this.log(`  ${dim('─'.repeat(45))}`)
    this.log('')

    const ready = await ask(`  ${dim('Ready to enter a proxy URL?')} ${dim('(y/N)')}: `)
    if (ready.toLowerCase() === 'y') {
      await this.optionPasteProxy()
    } else {
      this.log(`  ${dim('No problem. Run')} ${cyan('rift setup proxy')} ${dim('when you\'re ready.')}`)
    }
  }

  private async optionClearProxy(): Promise<void> {
    await clearProxy()
    this.log('')
    this.log(`  ${green('✔')} Proxy config removed. RIFT will connect directly.`)
  }
}
