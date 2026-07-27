import { expect, test } from 'bun:test'
import { graphifyStatusColor, graphifyStatusLabel, normalizeGraphifyStatus } from './graphifyStatus'

test('normalizes optional Graphify app status without exposing unknown fields', () => {
  const status = normalizeGraphifyStatus({
    mode: 'auto',
    state: 'ready',
    version: '0.9.27',
    freshness: 'fresh',
    node_count: 5098,
    edge_count: 22091,
    report_path: '/tmp/project/.tmux_workflow/evidence.md',
    executable_path: '/secret/graphify',
    cache_dir: '/secret/cache',
  })

  expect(status).toMatchObject({
    mode: 'auto',
    state: 'ready',
    version: '0.9.27',
    freshness: 'fresh',
    nodeCount: 5098,
    edgeCount: 22091,
    reportPath: '/tmp/project/.tmux_workflow/evidence.md',
  })
  expect(status && 'executablePath' in status).toBe(false)
  expect(status && 'cacheDir' in status).toBe(false)
  expect(graphifyStatusLabel(status)).toBe('Ready')
  expect(graphifyStatusColor(status)).toBe('#00d2ff')
})

test('keeps legacy snapshots absent and safely degrades unknown states', () => {
  expect(normalizeGraphifyStatus(undefined)).toBeUndefined()
  const degraded = normalizeGraphifyStatus({ mode: 'auto', state: 'future-state' })
  expect(degraded?.state).toBe('degraded')
  expect(graphifyStatusLabel(degraded)).toBe('Degraded')
  expect(graphifyStatusColor(degraded)).toBe('#f7c948')
})
