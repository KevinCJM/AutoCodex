import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

type BackendEnvelope = {
  kind: 'request' | 'response' | 'event'
  id: string
  version: string
  action?: string
  type?: string
  ok?: boolean
  payload?: Record<string, unknown>
  error?: string
}

export type BackendEvent = {
  type: string
  payload: Record<string, unknown>
}

export function repoRoot() {
  return resolve(import.meta.dir, '../../../..')
}

export function readPythonPath() {
  const configPath = join(repoRoot(), 'U01_common_config.py')
  const content = readFileSync(configPath, 'utf8')
  const match = content.match(/SYSTEM_PYTHON_PATH\s*=\s*"([^"]+)"/)
  if (!match) {
    throw new Error(`Cannot resolve SYSTEM_PYTHON_PATH from ${configPath}`)
  }
  return String(match[1])
}

export class BackendClient {
  private process?: Bun.Subprocess<'pipe', 'pipe', 'pipe'>
  private nextId = 1
  private buffer = ''
  private pending = new Map<string, { resolve: (value: unknown) => void; reject: (reason?: unknown) => void }>()
  private listeners = new Set<(event: BackendEvent) => void>()

  async start() {
    if (this.process) return
    const python = readPythonPath()
    const backendPath = join(repoRoot(), 'T11_tui_backend.py')
    this.process = Bun.spawn([python, backendPath], {
      cwd: repoRoot(),
      stdin: 'pipe',
      stdout: 'pipe',
      stderr: 'pipe',
      env: process.env,
    })
    this.consumeStream(this.process.stdout)
    this.consumeStderr(this.process.stderr)
  }

  stop() {
    const pending = [...this.pending.values()]
    this.pending.clear()
    for (const waiter of pending) {
      waiter.reject(new Error('backend stopped'))
    }
    this.listeners.clear()
    const child = this.process
    this.process = undefined
    if (!child) return
    child.kill()
  }

  private async consumeStderr(stream: ReadableStream<Uint8Array>) {
    const reader = stream.getReader()
    const decoder = new TextDecoder()
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const text = decoder.decode(value)
      this.emit({ type: 'log.append', payload: { text } })
    }
  }

  private async consumeStream(stream: ReadableStream<Uint8Array>) {
    const reader = stream.getReader()
    const decoder = new TextDecoder()
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      this.buffer += decoder.decode(value, { stream: true })
      let index = this.buffer.indexOf('\n')
      while (index >= 0) {
        const line = this.buffer.slice(0, index).trim()
        this.buffer = this.buffer.slice(index + 1)
        if (line) this.handleLine(line)
        index = this.buffer.indexOf('\n')
      }
    }
  }

  private handleLine(line: string) {
    let message: BackendEnvelope
    try {
      message = JSON.parse(line) as BackendEnvelope
    } catch {
      this.emit({ type: 'log.append', payload: { text: `${line}\n` } })
      return
    }
    if (message.kind === 'event') {
      this.emit({ type: String(message.type || 'unknown'), payload: { ...(message.payload || {}) } })
      return
    }
    if (message.kind === 'response') {
      const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      if (message.ok) {
        pending.resolve(message.payload || {})
      } else {
        pending.reject(new Error(String(message.error || 'backend request failed')))
      }
    }
  }

  private emit(event: BackendEvent) {
    for (const listener of this.listeners) {
      listener(event)
    }
  }

  subscribe(listener: (event: BackendEvent) => void) {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async request(action: string, payload: Record<string, unknown> = {}) {
    await this.start()
    const id = `req_${this.nextId++}`
    const envelope = {
      kind: 'request',
      id,
      version: '1.0',
      action,
      payload,
    }
    const waiter = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
    })
    this.process!.stdin.write(JSON.stringify(envelope) + '\n')
    return waiter
  }

  async bootstrap() {
    return this.request('app.bootstrap', {})
  }

  async submitPrompt(promptId: string, value: unknown) {
    return this.request('prompt.response', {
      prompt_id: promptId,
      value,
    })
  }

  sendPresence(reason: string, shellFocus: string): void {
    void this.request('ui.presence', {
      reason,
      shell_focus: shellFocus,
    }).catch(() => undefined)
  }
}
