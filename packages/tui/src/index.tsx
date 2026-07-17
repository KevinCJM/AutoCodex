import { createCliRenderer } from '@opentui/core'
import { render } from '@opentui/solid'
import {
  App,
  claimBackendShutdownOwnership,
  getLatestBackendCleanupContext,
  requestBackendCleanupShutdown,
  runBackendCleanupFallback,
  setInitialBackendCleanupContext,
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

function readOption(argv: string[], name: string) {
  const index = argv.indexOf(name)
  return index >= 0 ? String(argv[index + 1] ?? '').trim() : ''
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

async function stopBackendAndCleanup(reason: 'signal' | 'runner_failure') {
  const cleanupContext = getLatestBackendCleanupContext()
  try {
    await requestBackendCleanupShutdown(reason)
  } catch {
    // The backend may already be unavailable; cleanup-only remains authoritative.
  }
  try {
    await stopBackendClient({ reason, forceKillAfterMs: 30000 })
  } catch {
    // Continue with the independent cleanup pass.
  } finally {
    try {
      // Idempotent second pass covers backend disconnects, cleanup probe errors,
      // and forced termination before the backend's finally block completed.
      await runBackendCleanupFallback(cleanupContext)
    } catch {
      // Exit must still complete when tmux itself is unavailable.
    }
  }
}

async function shutdownFromSignal(signal: ShutdownSignal) {
  if (shutdownStarted) return
  shutdownStarted = true
  claimBackendShutdownOwnership()
  try {
    renderer.destroy()
  } catch {
    // Renderer may already be shutting down.
  }
  await stopBackendAndCleanup('signal')
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
    await stopBackendAndCleanup('runner_failure')
  } finally {
    process.exit(1)
  }
}

for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) {
  process.on(signal, () => {
    void shutdownFromSignal(signal)
  })
}

const startup = parseStartupArgs(Bun.argv.slice(2))
setInitialBackendCleanupContext({
  projectDir: readOption(startup.initialArgv, '--project-dir'),
  requirementName: readOption(startup.initialArgv, '--requirement-name'),
  action: startup.action,
})

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
