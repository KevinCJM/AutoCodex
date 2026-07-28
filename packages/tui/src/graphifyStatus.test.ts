import { expect, test } from 'bun:test'
import { graphifyStatusColor, graphifyStatusLabel, graphifyStatusSummary, normalizeGraphifyStatus } from './graphifyStatus'

test('normalizes optional Graphify app status without exposing unknown fields', () => {
  const status = normalizeGraphifyStatus({
    mode: 'auto',
    state: 'ready',
    version: '0.9.27',
    freshness: 'fresh',
    node_count: 5098,
    edge_count: 22091,
    report_path: '/tmp/project/.tmux_workflow/evidence.md',
    query_count_stage: 4,
    last_query_command: 'affected',
    last_query_at: '2026-07-27T10:11:12+08:00',
    last_query_status: 'ok',
    last_query_freshness: 'fresh',
    last_query_truncated: false,
    query_text: 'private question',
    query_result: 'private result',
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
    queryCountStage: 4,
    lastQueryCommand: 'affected',
    lastQueryFreshness: 'fresh',
    lastQueryTruncated: false,
  })
  expect(status && 'executablePath' in status).toBe(false)
  expect(status && 'cacheDir' in status).toBe(false)
  expect(status && 'queryText' in status).toBe(false)
  expect(status && 'queryResult' in status).toBe(false)
  expect(graphifyStatusLabel(status)).toBe('Ready')
  expect(graphifyStatusSummary(status)).toBe('Ready · v0.9.27 · 本阶段 4 次查询 · 最近 affected/fresh')
  expect(graphifyStatusColor(status)).toBe('#00d2ff')
})

test('keeps legacy snapshots absent and safely degrades unknown states', () => {
  expect(normalizeGraphifyStatus(undefined)).toBeUndefined()
  const degraded = normalizeGraphifyStatus({ mode: 'auto', state: 'future-state' })
  expect(degraded?.state).toBe('degraded')
  expect(graphifyStatusLabel(degraded)).toBe('Degraded')
  expect(graphifyStatusSummary(normalizeGraphifyStatus({ mode: 'auto', state: 'ready', version: '0.9.27' }))).toBe('Ready · v0.9.27')
  expect(graphifyStatusColor(degraded)).toBe('#f7c948')
})

test('drops invalid query metadata instead of reflecting arbitrary text', () => {
  const status = normalizeGraphifyStatus({
    mode: 'auto',
    state: 'ready',
    query_count_stage: -4,
    last_query_command: '/Users/private/query',
    last_query_at: '/Users/private/time',
    last_query_status: 'secret result',
    last_query_freshness: 'private question',
    last_query_truncated: 'true',
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
