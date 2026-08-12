import { expect, test } from 'bun:test'
import { codegraphStatusLabel, codegraphStatusSummary, codegraphStatusTone, normalizeCodeGraphStatus } from './codegraphStatus'

test('normalizes CodeGraph status with shared display semantics', () => {
  const status = normalizeCodeGraphStatus({
    mode: 'auto', state: 'stale', version: '1.5.0', initialized: true,
    node_count: 12, edge_count: 34, pending_changes: { added: 1, modified: 1, removed: 1 },
    executable_path: '/secret/codegraph', index_path: '/secret/.codegraph',
  })
  expect(status).toMatchObject({ mode: 'auto', state: 'stale', version: '1.5.0', nodeCount: 12, pendingChanges: { added: 1, modified: 1, removed: 1 } })
  expect(status && 'executablePath' in status).toBe(false)
  expect(codegraphStatusLabel(status)).toBe('Stale')
  expect(codegraphStatusSummary(status)).toBe('Stale · v1.5.0 · 3 files pending')
  expect(codegraphStatusTone(status)).toBe('active')
})

test('keeps missing snapshots absent and unknown states degraded', () => {
  expect(normalizeCodeGraphStatus(undefined)).toBeUndefined()
  const status = normalizeCodeGraphStatus({ mode: 'auto', state: 'future' })
  expect(status?.state).toBe('degraded')
  expect(codegraphStatusTone(status)).toBe('active')
})

test('uses the same prioritized degraded reasons as the TUI', () => {
  const base = { mode: 'auto', state: 'degraded', index_state: 'complete' }
  expect(codegraphStatusSummary(normalizeCodeGraphStatus({ ...base, worktree_mismatch: true, reindex_recommended: true, pending_refs: 3 })))
    .toBe('Degraded · worktree mismatch')
  expect(codegraphStatusSummary(normalizeCodeGraphStatus({ ...base, reindex_recommended: true, pending_refs: 3 })))
    .toBe('Degraded · reindex recommended')
  expect(codegraphStatusSummary(normalizeCodeGraphStatus({ ...base, pending_refs: 3 })))
    .toBe('Degraded · 3 pending refs')
  expect(codegraphStatusSummary(normalizeCodeGraphStatus({ ...base, index_state: 'partial' })))
    .toBe('Degraded · partial index')
  expect(codegraphStatusSummary(normalizeCodeGraphStatus(base)))
    .toBe('Degraded · status unavailable')
})
