import { expect, test } from 'bun:test'
import { graphifyStatusLabel, graphifyStatusTone, normalizeGraphifyStatus } from './graphifyStatus'

test('normalizes Graphify app status with the shared display states', () => {
  const status = normalizeGraphifyStatus({
    mode: 'auto',
    state: 'stale',
    version: '0.9.27',
    node_count: 12,
    edge_count: 34,
    report_path: '/tmp/project/report.md',
    executable_path: '/secret/graphify',
  })

  expect(status).toMatchObject({
    mode: 'auto',
    state: 'stale',
    version: '0.9.27',
    nodeCount: 12,
    edgeCount: 34,
    reportPath: '/tmp/project/report.md',
  })
  expect(status && 'executablePath' in status).toBe(false)
  expect(graphifyStatusLabel(status)).toBe('Stale')
  expect(graphifyStatusTone(status)).toBe('active')
})

test('keeps legacy snapshots absent and treats unknown states as degraded', () => {
  expect(normalizeGraphifyStatus(undefined)).toBeUndefined()
  const status = normalizeGraphifyStatus({ mode: 'auto', state: 'future-state' })
  expect(status?.state).toBe('degraded')
  expect(graphifyStatusLabel(status)).toBe('Degraded')
  expect(graphifyStatusTone(status)).toBe('active')
})
