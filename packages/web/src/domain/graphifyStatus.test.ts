import { expect, test } from 'bun:test'
import { graphifyStatusLabel, graphifyStatusSummary, graphifyStatusTone, normalizeGraphifyStatus } from './graphifyStatus'

test('normalizes Graphify app status with the shared display states', () => {
  const status = normalizeGraphifyStatus({
    mode: 'auto',
    state: 'stale',
    version: '0.9.27',
    node_count: 12,
    edge_count: 34,
    report_path: '/tmp/project/report.md',
    query_count_stage: 2,
    last_query_command: 'path',
    last_query_at: '2026-07-27T10:11:12Z',
    last_query_status: 'ok',
    last_query_freshness: 'stale',
    last_query_truncated: true,
    executable_path: '/secret/graphify',
  })

  expect(status).toMatchObject({
    mode: 'auto',
    state: 'stale',
    version: '0.9.27',
    nodeCount: 12,
    edgeCount: 34,
    reportPath: '/tmp/project/report.md',
    queryCountStage: 2,
    lastQueryCommand: 'path',
    lastQueryFreshness: 'stale',
    lastQueryTruncated: true,
  })
  expect(status && 'executablePath' in status).toBe(false)
  expect(graphifyStatusLabel(status)).toBe('Stale')
  expect(graphifyStatusSummary(status)).toBe('Stale · v0.9.27 · 本阶段 2 次查询 · 最近 path/stale')
  expect(graphifyStatusTone(status)).toBe('active')
})

test('keeps legacy snapshots absent and treats unknown states as degraded', () => {
  expect(normalizeGraphifyStatus(undefined)).toBeUndefined()
  const status = normalizeGraphifyStatus({ mode: 'auto', state: 'future-state' })
  expect(status?.state).toBe('degraded')
  expect(graphifyStatusLabel(status)).toBe('Degraded')
  expect(graphifyStatusTone(status)).toBe('active')
  expect(graphifyStatusSummary(normalizeGraphifyStatus({ mode: 'auto', state: 'ready', version: '0.9.27' }))).toBe('Ready · v0.9.27')
})

test('drops invalid query metadata instead of reflecting arbitrary text', () => {
  const status = normalizeGraphifyStatus({
    mode: 'auto',
    state: 'ready',
    query_count_stage: -1,
    last_query_command: '/secret/path',
    last_query_at: 'private question',
    last_query_status: 'private result',
    last_query_freshness: 'private freshness',
    last_query_truncated: 1,
  })
  expect(status).toMatchObject({
    queryCountStage: 0,
    lastQueryCommand: '',
    lastQueryAt: '',
    lastQueryStatus: '',
    lastQueryFreshness: '',
    lastQueryTruncated: false,
  })
})
