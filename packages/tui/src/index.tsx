import { createCliRenderer } from '@opentui/core'
import { render } from '@opentui/solid'
import {
  App,
  claimBackendShutdownOwnership,
  getLatestBackendCleanupContext,
  requestBackendPreserveOrphansShutdown,
  runBackendCleanupFallback,
  stopBackendClient,
} from './app'
import { copyToClipboard } from './clipboard'
import { formatStageFailureReport } from './terminalFailure'
import type { StageFailureSnapshot } from './types'

type StartupRoute = 'home' | 'routing' | 'requirements' | 'review' | 'design' | 'task-split' | 'development' | 'overall-review' | 'control'
type ShutdownSignal = 'SIGINT' | 'SIGTERM' | 'SIGHUP'

function parseStartupArgs(argv: string[]) {
  let route: StartupRoute | undefined
  let action = ''
  let initialArgv: string[] = []
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index]
    if (item === '--route') {
      const value = argv[index + 1]
      if (
        value === 'home'
        || value === 'routing'
        || value === 'requirements'
        || value === 'review'
        || value === 'design'
        || value === 'task-split'
        || value === 'development'
        || value === 'overall-review'
        || value === 'control'
      ) {
        route = value
      }
      index += 1
      continue
    }
    if (item === '--action') {
      action = argv[index + 1] ?? ''
      index += 1
      continue
    }
    if (item === '--argv-json') {
      const raw = argv[index + 1] ?? '[]'
      try {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed)) {
          initialArgv = parsed.map((entry) => String(entry))
        }
      } catch {
        initialArgv = []
      }
      index += 1
    }
  }
  return {
    route,
    action: action || undefined,
    initialArgv,
  }
}

const renderer = await createCliRenderer({
  targetFps: 60,
  exitOnCtrlC: false,
  useMouse: true,
  useKittyKeyboard: { disambiguate: true, alternateKeys: true, allKeysAsEscapes: true },
  autoFocus: true,
  screenMode: 'alternate-screen',
  externalOutputMode: 'passthrough',
  consoleMode: 'disabled',
  openConsoleOnError: false,
  consoleOptions: {
    keyBindings: [{ name: 'y', ctrl: true, action: 'copy-selection' }],
    onCopySelection: (text) => {
      if (!text) return
      void copyToClipboard(text)
    },
  },
})

let shutdownStarted = false

function exitCodeForSignal(signal: ShutdownSignal) {
  if (signal === 'SIGINT') return 130
  if (signal === 'SIGTERM') return 143
  if (signal === 'SIGHUP') return 129
  return 1
}

async function shutdownFromSignal(signal: ShutdownSignal) {
  if (shutdownStarted) return
  shutdownStarted = true
  try {
    renderer.destroy()
  } catch {
    // Renderer may already be shutting down.
  }
  const stopResult = await stopBackendClient({ reason: 'signal', forceKillAfterMs: 30000 })
  if (!stopResult.graceful || stopResult.signalEscalatedToSigkill) {
    await runBackendCleanupFallback(getLatestBackendCleanupContext())
  }
  process.exit(exitCodeForSignal(signal))
}

async function shutdownFromTerminalFailure(failure: StageFailureSnapshot) {
  if (shutdownStarted) return
  shutdownStarted = true
  claimBackendShutdownOwnership()
  try {
    renderer.destroy()
  } catch {
    // Renderer may already be shutting down.
  }
  try {
    process.stderr.write(formatStageFailureReport(failure))
  } catch {
    // A closed stderr must not prevent failure shutdown.
  }
  try {
    await requestBackendPreserveOrphansShutdown()
  } catch {
    // The backend already latches preserve-orphans before emitting runner_failure.
  }
  try {
    await stopBackendClient({ reason: 'runner_failure', forceKillAfterMs: 30000 })
  } finally {
    // Failure shutdown deliberately has no cleanup-only fallback: the tmux sessions
    // are the diagnostic evidence the user was promised would be preserved.
    process.exit(1)
  }
}

for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) {
  process.on(signal, () => {
    void shutdownFromSignal(signal)
  })
}

const startup = parseStartupArgs(Bun.argv.slice(2))

await render(
  () => (
    <App
      initialRoute={startup.route}
      initialAction={startup.action}
      initialArgv={startup.initialArgv}
      onExitRequest={() => shutdownFromSignal('SIGINT')}
      onTerminalFailure={shutdownFromTerminalFailure}
    />
  ),
  renderer,
)
