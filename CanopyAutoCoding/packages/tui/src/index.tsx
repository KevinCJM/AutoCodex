import { createCliRenderer } from '@opentui/core'
import { render } from '@opentui/solid'
import { App } from './app'
import { copyToClipboard } from './clipboard'

type StartupRoute = 'home' | 'routing' | 'requirements' | 'review' | 'design' | 'task-split' | 'development' | 'overall-review' | 'control'

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
  exitOnCtrlC: true,
  useMouse: true,
  autoFocus: true,
  screenMode: 'alternate-screen',
  externalOutputMode: 'passthrough',
  consoleOptions: {
    keyBindings: [{ name: 'y', ctrl: true, action: 'copy-selection' }],
    onCopySelection: (text) => {
      if (!text) return
      void copyToClipboard(text)
    },
  },
})

const startup = parseStartupArgs(Bun.argv.slice(2))

await render(
  () => <App initialRoute={startup.route} initialAction={startup.action} initialArgv={startup.initialArgv} />,
  renderer,
)
