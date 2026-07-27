import { expect, test } from 'bun:test'
import { connectBridgeEvents, getRequirements, parseBridgeEvent, submitPromptResponse } from './client'

class FakeEventSource {
  listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>()
  closed = false

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    const list = this.listeners.get(type) ?? []
    list.push(listener)
    this.listeners.set(type, list)
  }

  emit(type: string, payload: Record<string, unknown>): void {
    const event = { data: JSON.stringify({ type, payload }) } as MessageEvent<string>
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event)
    }
  }

  close(): void {
    this.closed = true
  }
}

test('parseBridgeEvent decodes named SSE payload', () => {
  const event = parseBridgeEvent(JSON.stringify({ type: 'snapshot.app', payload: { active_stage: 'stage.a07.start' } }))

  expect(event.type).toBe('snapshot.app')
  expect(event.payload.active_stage).toBe('stage.a07.start')
})

test('connectBridgeEvents registers named bridge events', () => {
  const fake = new FakeEventSource()
  const received: string[] = []
  const disconnect = connectBridgeEvents(
    (event) => received.push(event.type),
    { eventSourceFactory: () => fake },
  )

  fake.emit('log.append', { text: 'hello' })
  fake.emit('snapshot.stage', { route: 'development' })
  fake.emit('prompt.request', { id: 'prompt_1' })
  fake.emit('snapshot.prompt', { pending: true, prompt_id: 'prompt_1' })
  disconnect()

  expect(received).toEqual(['log.append', 'snapshot.stage', 'prompt.request', 'snapshot.prompt'])
  expect(fake.closed).toBe(true)
})

test('getRequirements reports an empty backend response clearly', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async () => new Response('', { status: 502 })) as unknown as typeof fetch
  try {
    await expect(getRequirements('/tmp/project')).rejects.toThrow('后端响应为空')
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('getRequirements reports non-json backend response clearly', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async () => new Response('proxy error', { status: 500 })) as unknown as typeof fetch
  try {
    await expect(getRequirements('/tmp/project')).rejects.toThrow('后端返回非 JSON 响应')
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('submitPromptResponse carries the Grill cursor and keeps legacy calls unchanged', async () => {
  const originalFetch = globalThis.fetch
  const bodies: Array<Record<string, unknown>> = []
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)
    return new Response(JSON.stringify({ ok: true, payload: { accepted: true } }), { status: 200 })
  }) as typeof fetch
  try {
    await submitPromptResponse('prompt-grill', 'A', {
      runnerId: 'runner-a03',
      questionSeq: 3,
      grillSessionId: 'session-a',
      grillQuestionHash: 'sha256:abc',
    })
    await submitPromptResponse('prompt-standard', 'legacy')
  } finally {
    globalThis.fetch = originalFetch
  }

  expect(bodies).toEqual([
    {
      prompt_id: 'prompt-grill',
      value: 'A',
      runner_id: 'runner-a03',
      question_seq: 3,
      grill_session_id: 'session-a',
      grill_question_hash: 'sha256:abc',
    },
    { prompt_id: 'prompt-standard', value: 'legacy' },
  ])
})
