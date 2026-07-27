import { expect, test } from 'bun:test'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { BackendClient, readPythonPath, repoRoot, runCleanupOnlyBackend } from './client'

test('BackendClient can be constructed', () => {
  const client = new BackendClient()
  expect(client).toBeInstanceOf(BackendClient)
})

test('BackendClient submits Grill cursor while keeping legacy prompt responses compatible', async () => {
  const client = new BackendClient() as any
  const requests: Array<{ action: string; payload: Record<string, unknown> }> = []
  client.request = async (action: string, payload: Record<string, unknown>) => {
    requests.push({ action, payload })
    return { accepted: true }
  }

  await client.submitPrompt('prompt-grill', 'A', {
    runnerId: 'runner-a03',
    questionSeq: 5,
    grillSessionId: 'session-a',
    grillQuestionHash: 'sha256:abc',
  })
  await client.submitPrompt('prompt-standard', 'legacy')

  expect(requests).toEqual([
    {
      action: 'prompt.response',
      payload: {
        prompt_id: 'prompt-grill',
        value: 'A',
        runner_id: 'runner-a03',
        question_seq: 5,
        grill_session_id: 'session-a',
        grill_question_hash: 'sha256:abc',
      },
    },
    {
      action: 'prompt.response',
      payload: { prompt_id: 'prompt-standard', value: 'legacy' },
    },
  ])
})

test('backend client resolves repo root and python config from repository root', () => {
  const root = repoRoot()
  expect(existsSync(join(root, 'U01_common_config.py'))).toBe(true)
  expect(readPythonPath().length).toBeGreaterThan(0)
})

test('BackendClient treats non-JSON stdout lines as log events instead of crashing', () => {
  const events: Array<{ type: string; payload: Record<string, unknown> }> = []
  const client = new BackendClient() as any
  client.subscribe((event: { type: string; payload: Record<string, unknown> }) => {
    events.push(event)
  })
  client.handleLine('警告：文件不存在 -> /tmp/demo')
  expect(events).toHaveLength(1)
  expect(events[0]?.type).toBe('log.append')
  expect(String(events[0]?.payload.text ?? '')).toContain('警告：文件不存在')
})

test('BackendClient stop tears down child process and completes pending requests', async () => {
  const client = new BackendClient() as any
  const signals: string[] = []
  let resolved = false
  client.process = {
    kill: (signal?: string) => {
      signals.push(signal || '')
    },
  }
  client.pending.set('req_1', {
    resolve: () => {
      resolved = true
    },
    reject: () => {
      throw new Error('shutdown should not reject pending requests')
    },
  })
  client.subscribe(() => undefined)

  const result = await client.stop()

  expect(signals).toEqual(['SIGTERM'])
  expect(result).toEqual({ graceful: true, signalEscalatedToSigkill: false })
  expect(resolved).toBe(true)
  expect(client.process).toBeUndefined()
  expect(client.pending.size).toBe(0)
  expect(client.listeners.size).toBe(0)
})

test('BackendClient stop waits for backend exit before completing', async () => {
  const client = new BackendClient() as any
  const signals: string[] = []
  let resolveExited!: () => void
  client.process = {
    kill: (signal?: string) => {
      signals.push(signal || '')
    },
    exited: new Promise<void>((resolve) => {
      resolveExited = resolve
    }),
  }

  let stopped = false
  const stopping = client.stop({ forceKillAfterMs: 50 }).then((result: { graceful: boolean }) => {
    expect(result.graceful).toBe(true)
    stopped = true
  })
  await Promise.resolve()

  expect(signals).toEqual(['SIGTERM'])
  expect(stopped).toBe(false)

  resolveExited()
  await stopping

  expect(stopped).toBe(true)
  expect(signals).toEqual(['SIGTERM'])
})

test('BackendClient stop escalates to SIGKILL when backend does not exit', async () => {
  const client = new BackendClient() as any
  const signals: string[] = []
  let resolveExited!: () => void
  client.process = {
    kill: (signal?: string) => {
      signals.push(signal || '')
      if (signal === 'SIGKILL') resolveExited()
    },
    exited: new Promise<void>((resolve) => {
      resolveExited = resolve
    }),
  }

  let result: { graceful: boolean; signalEscalatedToSigkill: boolean } | undefined
  const stopping = client.stop({ forceKillAfterMs: 1, reason: 'signal' }).then((value: { graceful: boolean; signalEscalatedToSigkill: boolean }) => {
    result = value
  })
  await stopping

  expect(signals).toEqual(['SIGTERM', 'SIGKILL'])
  expect(result).toEqual({ graceful: false, signalEscalatedToSigkill: true })
  client.stoppingProcess = undefined
  client.clearProcessExitHandlerIfIdle()
})

test('BackendClient process exit fallback sends SIGTERM only', () => {
  const client = new BackendClient() as any
  const signals: string[] = []
  client.process = {
    kill: (signal?: string) => {
      signals.push(signal || '')
    },
  }

  client.ensureProcessExitHandler()
  client.processExitHandler()
  client.process = undefined
  client.clearProcessExitHandlerIfIdle()

  expect(signals).toEqual(['SIGTERM'])
})

test('BackendClient rejects pending requests and emits a terminal event on unexpected stdout EOF', async () => {
  const client = new BackendClient() as any
  const child = {}
  const events: Array<{ type: string; payload: Record<string, unknown> }> = []
  let rejectedMessage = ''
  client.process = child
  client.pending.set('req_1', {
    resolve: () => {
      throw new Error('unexpected disconnect must not resolve pending requests')
    },
    reject: (error: Error) => {
      rejectedMessage = error.message
    },
  })
  client.subscribe((event: { type: string; payload: Record<string, unknown> }) => events.push(event))
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.close()
    },
  })

  await client.consumeStream(stream, child)

  expect(rejectedMessage).toContain('stdout closed unexpectedly')
  expect(client.pending.size).toBe(0)
  expect(events).toEqual([{
    type: 'backend.disconnected',
    payload: { message: 'backend stdout closed unexpectedly' },
  }])
})

test('BackendClient ignores child exit notification after an intentional stop begins', () => {
  const client = new BackendClient() as any
  const child = {}
  const events: unknown[] = []
  client.process = child
  client.stoppingProcess = child
  client.subscribe((event: unknown) => events.push(event))

  client.handleUnexpectedDisconnect(child, 'backend exited with code 143', 143)

  expect(events).toEqual([])
  expect(client.backendDisconnectError).toBeUndefined()
})

test('BackendClient requests cleanup policy before failure shutdown', async () => {
  const client = new BackendClient() as any
  client.process = { stdin: { write: () => undefined } }
  const requests: Array<{ action: string; payload: Record<string, unknown> }> = []
  client.request = async (action: string, payload: Record<string, unknown>) => {
    requests.push({ action, payload })
    return { accepted: true }
  }

  expect(await client.requestShutdownPolicy('cleanup', 'runner_failure', 10)).toBe(true)
  expect(requests).toEqual([{
    action: 'app.shutdown',
    payload: { policy: 'cleanup', reason: 'runner_failure' },
  }])
})

test('cleanup-only backend runner skips spawn when project dir is missing', async () => {
  const ok = await runCleanupOnlyBackend({ projectDir: '' })
  expect(ok).toBe(false)
})
