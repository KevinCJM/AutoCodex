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

const BACKEND_STOP_KILL_GRACE_MS = 10000
const BACKEND_CLEANUP_ONLY_EXIT_TIMEOUT_MS = 30000
const BACKEND_SHUTDOWN_POLICY_TIMEOUT_MS = 1500

export type BackendEvent = {
  type: string
  payload: Record<string, unknown>
}

export type BackendStopOptions = {
  forceKillAfterMs?: number
  reason?: 'normal' | 'signal' | 'runner_failure'
}

export type BackendStopResult = {
  graceful: boolean
  signalEscalatedToSigkill: boolean
}

export type BackendCleanupContext = {
  projectDir: string
  requirementName?: string
  action?: string
}

type BackendProcess = Bun.Subprocess<'pipe', 'pipe', 'pipe'>

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

export async function runCleanupOnlyBackend(context: BackendCleanupContext): Promise<boolean> {
  const projectDir = String(context.projectDir || '').trim()
  if (!projectDir) return false
  const python = readPythonPath()
  const backendPath = join(repoRoot(), 'T11_tui_backend.py')
  const command = [python, backendPath, '--cleanup-project-dir', projectDir]
  const requirementName = String(context.requirementName || '').trim()
  const action = String(context.action || '').trim()
  if (requirementName) {
    command.push('--cleanup-requirement-name', requirementName)
  }
  if (action) {
    command.push('--cleanup-action', action)
  }
  const child = Bun.spawn(command, {
    cwd: repoRoot(),
    stdin: 'ignore',
    stdout: 'inherit',
    stderr: 'inherit',
    env: process.env,
  })
  let timeoutId: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<number>((resolve) => {
    timeoutId = setTimeout(() => resolve(124), BACKEND_CLEANUP_ONLY_EXIT_TIMEOUT_MS)
  })
  const exitCode = await Promise.race([
    Promise.resolve(child.exited).then((code) => Number(code ?? 0), () => 1),
    timeout,
  ])
  if (timeoutId) clearTimeout(timeoutId)
  if (exitCode === 124) {
    try {
      child.kill('SIGKILL')
    } catch {
      // Child already exited.
    }
  }
  return exitCode === 0
}

export class BackendClient {
  private process?: BackendProcess
  private stoppingProcess?: BackendProcess
  private stoppingPromise?: Promise<BackendStopResult>
  private disconnectedProcess?: BackendProcess
  private backendDisconnectError?: Error
  private processExitHandler?: () => void
  private nextId = 1
  private buffer = ''
  private pending = new Map<string, { resolve: (value: unknown) => void; reject: (reason?: unknown) => void }>()
  private listeners = new Set<(event: BackendEvent) => void>()

  async start() {
    if (this.process) {
      if (this.backendDisconnectError) throw this.backendDisconnectError
      return
    }
    const python = readPythonPath()
    const backendPath = join(repoRoot(), 'T11_tui_backend.py')
    const child = Bun.spawn([python, backendPath], {
      cwd: repoRoot(),
      stdin: 'pipe',
      stdout: 'pipe',
      stderr: 'pipe',
      env: process.env,
    })
    this.process = child
    this.disconnectedProcess = undefined
    this.backendDisconnectError = undefined
    this.ensureProcessExitHandler()
    void this.consumeStream(child.stdout, child)
    void this.consumeStderr(child.stderr)
    void Promise.resolve(child.exited).then(
      (exitCode) => this.handleUnexpectedDisconnect(child, `backend exited with code ${Number(exitCode ?? 0)}`, Number(exitCode ?? 0)),
      (error) => this.handleUnexpectedDisconnect(child, `backend exit wait failed: ${String(error)}`),
    )
  }

  private handleUnexpectedDisconnect(child: BackendProcess, reason: string, exitCode?: number) {
    if (this.process !== child || this.stoppingProcess === child || this.disconnectedProcess === child) return
    this.disconnectedProcess = child
    const message = String(reason || 'backend disconnected').trim() || 'backend disconnected'
    const error = new Error(message)
    this.backendDisconnectError = error
    const pending = [...this.pending.values()]
    this.pending.clear()
    for (const waiter of pending) waiter.reject(error)
    this.emit({
      type: 'backend.disconnected',
      payload: {
        message,
        ...(exitCode === undefined ? {} : { exit_code: exitCode }),
      },
    })
  }

  private ensureProcessExitHandler() {
    if (this.processExitHandler) return
    this.processExitHandler = () => {
      const child = this.process ?? this.stoppingProcess
      if (!child) return
      try {
        child.kill('SIGTERM')
      } catch {
        // Process is already gone.
      }
    }
    process.on('exit', this.processExitHandler)
  }

  private clearProcessExitHandlerIfIdle() {
    if (this.process || this.stoppingProcess || !this.processExitHandler) return
    process.off('exit', this.processExitHandler)
    this.processExitHandler = undefined
  }

  async stop(options: BackendStopOptions = {}): Promise<BackendStopResult> {
    const forceKillAfterMs = options.forceKillAfterMs ?? BACKEND_STOP_KILL_GRACE_MS
    void options.reason
    const pending = [...this.pending.values()]
    this.pending.clear()
    for (const waiter of pending) {
      waiter.resolve({})
    }
    this.listeners.clear()
    const child = this.process
    this.process = undefined
    this.backendDisconnectError = undefined
    if (!child) {
      if (this.stoppingPromise) return await this.stoppingPromise
      return { graceful: true, signalEscalatedToSigkill: false }
    }
    if (this.stoppingProcess === child && this.stoppingPromise) {
      return await this.stoppingPromise
    }
    this.stoppingProcess = child
    try {
      child.kill('SIGTERM')
    } catch {
      this.stoppingProcess = undefined
      this.clearProcessExitHandlerIfIdle()
      return { graceful: true, signalEscalatedToSigkill: false }
    }
    const exited = child.exited
    if (!exited || typeof exited.finally !== 'function') {
      this.stoppingProcess = undefined
      this.clearProcessExitHandlerIfIdle()
      return { graceful: true, signalEscalatedToSigkill: false }
    }
    let signalEscalatedToSigkill = false
    const timer = setTimeout(() => {
      if (this.stoppingProcess !== child) return
      try {
        signalEscalatedToSigkill = true
        child.kill('SIGKILL')
      } catch {
        // Process is already gone.
      }
    }, Math.max(0, forceKillAfterMs))
    const stopPromise: Promise<BackendStopResult> = Promise.resolve(exited).then(
      () => ({
        graceful: !signalEscalatedToSigkill,
        signalEscalatedToSigkill,
      }),
      () => ({
        graceful: !signalEscalatedToSigkill,
        signalEscalatedToSigkill,
      }),
    ).finally(() => {
      if (timer) clearTimeout(timer)
      if (this.stoppingProcess === child) this.stoppingProcess = undefined
      if (this.stoppingPromise === stopPromise) this.stoppingPromise = undefined
      if (this.disconnectedProcess === child) this.disconnectedProcess = undefined
      this.clearProcessExitHandlerIfIdle()
    })
    this.stoppingPromise = stopPromise
    return await this.stoppingPromise
  }

  async requestShutdownPolicy(
    policy: 'cleanup',
    reason: string,
    timeoutMs = BACKEND_SHUTDOWN_POLICY_TIMEOUT_MS,
  ): Promise<boolean> {
    if (!this.process) return false
    let timeoutId: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<boolean>((resolve) => {
      timeoutId = setTimeout(() => resolve(false), Math.max(0, timeoutMs))
    })
    try {
      return await Promise.race([
        this.request('app.shutdown', { policy, reason }).then(() => true, () => false),
        timeout,
      ])
    } finally {
      if (timeoutId) clearTimeout(timeoutId)
    }
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

  private async consumeStream(stream: ReadableStream<Uint8Array>, child: BackendProcess) {
    const reader = stream.getReader()
    const decoder = new TextDecoder()
    try {
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
      this.handleUnexpectedDisconnect(child, 'backend stdout closed unexpectedly')
    } catch (error) {
      this.handleUnexpectedDisconnect(child, `backend stdout failed: ${String(error)}`)
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

  async submitPrompt(
    promptId: string,
    value: unknown,
    cursor?: {
      runnerId: string
      questionSeq: number
      grillSessionId?: string
      grillQuestionHash?: string
    },
  ) {
    return this.request('prompt.response', {
      prompt_id: promptId,
      value,
      ...(cursor ? {
        runner_id: cursor.runnerId,
        question_seq: cursor.questionSeq,
        ...(cursor.grillSessionId ? { grill_session_id: cursor.grillSessionId } : {}),
        ...(cursor.grillQuestionHash ? { grill_question_hash: cursor.grillQuestionHash } : {}),
      } : {}),
    })
  }

  sendPresence(reason: string, shellFocus: string): void {
    void this.request('ui.presence', {
      reason,
      shell_focus: shellFocus,
    }).catch(() => undefined)
  }
}
