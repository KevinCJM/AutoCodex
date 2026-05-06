import { expect, test } from 'bun:test'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { BackendClient, readPythonPath, repoRoot } from './client'

test('BackendClient can be constructed', () => {
  const client = new BackendClient()
  expect(client).toBeInstanceOf(BackendClient)
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

test('BackendClient stop tears down child process and pending requests', () => {
  const client = new BackendClient() as any
  let killed = false
  let rejected = false
  client.process = {
    kill: () => {
      killed = true
    },
  }
  client.pending.set('req_1', {
    resolve: () => undefined,
    reject: () => {
      rejected = true
    },
  })
  client.subscribe(() => undefined)

  client.stop()

  expect(killed).toBe(true)
  expect(rejected).toBe(true)
  expect(client.process).toBeUndefined()
  expect(client.pending.size).toBe(0)
  expect(client.listeners.size).toBe(0)
})
