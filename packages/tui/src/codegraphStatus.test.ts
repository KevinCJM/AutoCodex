import { expect, test } from 'bun:test'
import { codegraphStatusColor, codegraphStatusLabel, codegraphStatusSummary, normalizeCodeGraphStatus } from './codegraphStatus'

test('normalizes safe CodeGraph project status', () => {
  const status = normalizeCodeGraphStatus({
    mode: 'auto', state: 'ready', version: '1.5.0', initialized: true, freshness: 'fresh',
    last_indexed: '2026-08-10T10:00:00Z', file_count: 1200, node_count: 5098, edge_count: 22091,
    pending_changes: { added: 0, modified: 0, removed: 0 }, index_state: 'complete', pending_refs: 0,
    reindex_recommended: false, worktree_mismatch: false, executable_path: '/secret/codegraph', query_text: 'secret',
  })
  expect(status).toMatchObject({ mode: 'auto', state: 'ready', version: '1.5.0', initialized: true, fileCount: 1200, nodeCount: 5098 })
  expect(status && 'executablePath' in status).toBe(false)
  expect(status && 'queryText' in status).toBe(false)
  expect(codegraphStatusLabel(status)).toBe('Ready')
  expect(codegraphStatusSummary(status)).toBe('Ready · v1.5.0 · 5098 nodes')
  expect(codegraphStatusColor(status)).toBe('#00d2ff')
})

test('shows stale pending count and safely degrades unknown state', () => {
  const stale = normalizeCodeGraphStatus({ mode: 'auto', state: 'stale', version: '1.5.0', pending_changes: { added: 1, modified: 2, removed: 1 } })
  expect(codegraphStatusSummary(stale)).toBe('Stale · v1.5.0 · 4 files pending')
  const degraded = normalizeCodeGraphStatus({ mode: 'auto', state: 'future', index_state: 'partial' })
  expect(codegraphStatusSummary(degraded)).toBe('Degraded · partial index')
  expect(codegraphStatusColor(degraded)).toBe('#f7c948')
  expect(normalizeCodeGraphStatus(undefined)).toBeUndefined()
})

test('explains each degraded cause without claiming a complete index is the problem', () => {
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
