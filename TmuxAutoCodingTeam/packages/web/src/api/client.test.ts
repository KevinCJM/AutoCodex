import { expect, test } from 'bun:test'
import { connectBridgeEvents, getRequirements, parseBridgeEvent } from './client'

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
  disconnect()

  expect(received).toEqual(['log.append', 'snapshot.stage', 'prompt.request'])
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
