/**
 * Bridge between the TypeScript CLI and the Python engine.
 *
 * The CLI ships under three install paths:
 *
 *   1. Source clone — `engine/` directory exists above the CLI binary.
 *      We invoke `python -m rift.cli` with PYTHONPATH set to `engine/src`.
 *      Used during development.
 *
 *   2. Installed wheel — user did `pip install rift-engine-core`. The
 *      wheel ships a `rift-engine` console script as its entry point.
 *      We invoke that binary directly. Used for end-user pip/brew installs.
 *
 *   3. Env override — `RIFT_ENGINE_BINARY=/path/to/rift-engine` forces us
 *      to use a specific binary. Used by the Homebrew formula to point at
 *      the libexec venv's rift-engine even if PATH is weird.
 *
 * Detection runs in priority order: env override → source → installed.
 * Each spawned process inherits the same environment + extraEnv overlay.
 */

import {spawn, execSync, type ChildProcess} from 'node:child_process'
import {createInterface} from 'node:readline'
import * as path from 'node:path'
import * as fs from 'node:fs'
import * as os from 'node:os'
import {fileURLToPath} from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

export interface EngineMessage {
  type: 'progress' | 'result' | 'error' | 'status' | 'trade' | 'candle' | 'heartbeat' | 'shutdown' | 'step' | 'step_done' | 'soak'
  [key: string]: unknown
}

type EngineMode =
  | {kind: 'source'; engineDir: string}
  | {kind: 'installed'; binary: string}

/** Walk up looking for `engine/pyproject.toml` + `engine/src/rift/cli.py`.
 *  Only present in a source clone, not in any installed layout. */
function findSourceEngineDir(): string | null {
  let dir = path.resolve(__dirname)
  for (let i = 0; i < 10; i++) {
    const engineDir = path.join(dir, 'engine')
    // Both probes required to avoid matching `packages/engine/` (the
    // rift_engine library subpackage) before reaching the real `engine/`
    // host that owns `rift.cli`.
    if (
      fs.existsSync(path.join(engineDir, 'pyproject.toml')) &&
      fs.existsSync(path.join(engineDir, 'src', 'rift', 'cli.py'))
    ) return engineDir
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return null
}

/** Locate a binary on PATH via `which`. Returns null if not found. */
function findOnPath(binary: string): string | null {
  try {
    const out = execSync(`command -v ${binary}`, {encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore']}).trim()
    return out || null
  } catch {
    return null
  }
}

function detectEngine(): EngineMode {
  // 1. Env override
  const envBinary = process.env.RIFT_ENGINE_BINARY
  if (envBinary && fs.existsSync(envBinary)) {
    return {kind: 'installed', binary: envBinary}
  }

  // 2. Source clone
  const sourceDir = findSourceEngineDir()
  if (sourceDir) {
    return {kind: 'source', engineDir: sourceDir}
  }

  // 3. Installed wheel — rift-engine on PATH
  const binary = findOnPath('rift-engine')
  if (binary) {
    return {kind: 'installed', binary}
  }

  throw new Error(
    'Cannot find RIFT engine.\n' +
    '\n' +
    'Install one of:\n' +
    '  brew install Nexstone/tap/rift          # all-in-one, recommended\n' +
    '  pip install rift-engine-core            # Python engine only\n' +
    '\n' +
    'Or set RIFT_ENGINE_BINARY=/path/to/rift-engine to use a specific install.'
  )
}

/** Find Python for source mode. Prefers the engine's uv-managed venv. */
function findPythonForSource(engineDir: string): string {
  const engineVenv = path.join(engineDir, '.venv', 'bin', 'python3')
  if (fs.existsSync(engineVenv)) return engineVenv

  const dataVenv = path.join(getDataDir(), 'venv', 'bin', 'python3')
  if (fs.existsSync(dataVenv)) return dataVenv

  for (const cmd of ['python3.14', 'python3.13', 'python3']) {
    try {
      execSync(`${cmd} --version`, {stdio: 'ignore'})
      return cmd
    } catch {
      // try next
    }
  }

  throw new Error('Python 3.13+ not found. Install Python or run: rift setup')
}

function getStrategiesDir(engine: EngineMode): string {
  if (engine.kind === 'source') {
    // Walk up from the CLI's location looking for a `strategies/` sibling
    // to engine/. Falls back to <repo-root>/strategies.
    let dir = path.resolve(__dirname)
    for (let i = 0; i < 10; i++) {
      const stratDir = path.join(dir, 'strategies')
      if (fs.existsSync(stratDir)) return stratDir
      const parent = path.dirname(dir)
      if (parent === dir) break
      dir = parent
    }
    return path.join(engine.engineDir, '..', 'strategies')
  }

  // Installed mode: strategies live under the user's data dir. We create
  // it on demand so `rift new` and `rift algo` see the same path even
  // if the user never explicitly initialized it.
  const stratDir = path.join(getDataDir(), 'strategies')
  if (!fs.existsSync(stratDir)) {
    fs.mkdirSync(stratDir, {recursive: true})
  }
  return stratDir
}

export function getDataDir(): string {
  return path.join(process.env.HOME || os.homedir(), '.rift')
}

/** Public helper — install-mode-aware strategies dir, for commands that
 *  scaffold or list strategies. */
export function resolveStrategiesDir(): string {
  return getStrategiesDir(detectEngine())
}

const COMMANDS_WITH_STRATEGIES_DIR = new Set([
  'backtest', 'strategies', 'compare', 'walk-forward',
  'sweep', 'montecarlo', 'portfolio-backtest', 'research',
  'quick-test', 'algo',
])

interface SpawnPlan {
  cmd: string
  args: string[]
  env: NodeJS.ProcessEnv
  cwd?: string
}

function buildSpawnPlan(
  command: string,
  args: string[],
  extraEnv: Record<string, string> | undefined,
  engine: EngineMode,
): SpawnPlan {
  const finalArgs: string[] = []
  let cmd: string
  let cwd: string | undefined
  let env: NodeJS.ProcessEnv

  if (engine.kind === 'source') {
    const python = findPythonForSource(engine.engineDir)
    cmd = python
    finalArgs.push('-m', 'rift.cli')
    cwd = engine.engineDir
    env = {
      ...process.env,
      ...extraEnv,
      PYTHONPATH: path.join(engine.engineDir, 'src'),
      PYTHONUNBUFFERED: '1',
    }
  } else {
    cmd = engine.binary
    cwd = undefined // inherit caller's cwd
    env = {
      ...process.env,
      ...extraEnv,
      PYTHONUNBUFFERED: '1',
    }
  }

  finalArgs.push(command, ...args)

  if (COMMANDS_WITH_STRATEGIES_DIR.has(command)) {
    finalArgs.push('--strategies-dir', getStrategiesDir(engine))
  }

  return {cmd, args: finalArgs, env, cwd}
}

export async function runEngine(
  command: string,
  args: string[],
  onMessage: (msg: EngineMessage) => void,
  extraEnv?: Record<string, string>,
): Promise<void> {
  const engine = detectEngine()
  const plan = buildSpawnPlan(command, args, extraEnv, engine)

  const proc = spawn(plan.cmd, plan.args, {
    cwd: plan.cwd,
    env: plan.env,
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  const promise = new Promise<void>((resolve, reject) => {
    const rl = createInterface({input: proc.stdout!})
    rl.on('line', (line: string) => {
      try {
        const msg = JSON.parse(line) as EngineMessage
        onMessage(msg)
      } catch {
        // Non-JSON output — ignore
      }
    })

    let stderr = ''
    proc.stderr!.on('data', (chunk: Buffer) => {
      stderr += chunk.toString()
    })

    proc.on('close', (code: number | null) => {
      if (code === 0 || code === null) {
        resolve()
      } else {
        const lines = stderr.trim().split('\n').filter(l => l.trim())

        // Typer's click-rich style emits errors inside a unicode-box:
        //   ╭─ Error ──...──╮
        //   │ <message>     │
        //   ╰────...──────╯
        // Pull the message line out so the user sees "No such command X"
        // instead of the box-drawing close character that happens to be
        // the last stderr line.
        const typerBoxMsg = (() => {
          for (const l of lines) {
            const m = l.match(/^\s*│\s+(.+?)\s+│?\s*$/)
            if (m && m[1].trim()) return m[1].trim()
          }
          return null
        })()

        const lastLine = lines[lines.length - 1] || ''
        let errorLine =
          typerBoxMsg
          || lines.find(l => /^Error:/.test(l.trim()))
          || lines.find(l => /^(KeyError|ValueError|TypeError|RuntimeError|FileNotFoundError|ImportError|AttributeError|NameError):/.test(l.trim()))
          || lastLine
        errorLine = errorLine.trim().replace(/^Error:\s*/, '')
        reject(new Error(errorLine || `Engine exited with code ${code}`))
      }
    })

    proc.on('error', (err: Error) => {
      reject(new Error(`Failed to start engine: ${err.message}`))
    })
  })

  // Expose the child process for signal control (used by raw mode ESC handler)
  ;(promise as unknown as {_proc: ChildProcess})._proc = proc

  return promise
}

/** Get the child process from a runEngine promise (for sending signals) */
export function getEngineProcess(promise: Promise<void>): ChildProcess | null {
  return (promise as unknown as {_proc?: ChildProcess})._proc ?? null
}

/**
 * Spawn the Python engine as a detached daemon process.
 * Returns immediately after the process is started.
 * The daemon writes its own PID file and log file.
 */
export function spawnDaemon(
  command: string,
  args: string[],
  extraEnv?: Record<string, string>,
): {pid: number} {
  const engine = detectEngine()
  const plan = buildSpawnPlan(command, [...args, '--daemon'], extraEnv, engine)

  // Open /dev/null for stdio — daemon writes to its own log file
  const devNull = fs.openSync('/dev/null', 'r+')

  const proc = spawn(plan.cmd, plan.args, {
    cwd: plan.cwd,
    env: plan.env,
    stdio: [devNull, devNull, devNull],
    detached: true,
  })

  // Let the daemon run independently of this process
  proc.unref()

  const pid = proc.pid ?? 0
  fs.closeSync(devNull)

  return {pid}
}

/** Session directory paths (must match Python's ALGO_DIR layout) */
export function getAlgoSessionsDir(): string {
  return path.join(getDataDir(), 'algo', 'sessions')
}

export function getAlgoPidsDir(): string {
  return path.join(getDataDir(), 'algo', 'pids')
}

export function getAlgoLogsDir(): string {
  return path.join(getDataDir(), 'algo', 'logs')
}
