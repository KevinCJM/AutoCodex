import type { CodeGraphStatus } from './types'

const STATES = new Set(['off', 'unavailable', 'initializing', 'syncing', 'ready', 'stale', 'degraded', 'failed'])
const LABELS: Record<string, string> = {
  off: 'Off', unavailable: 'Unavailable', initializing: 'Initializing', syncing: 'Syncing',
  ready: 'Ready', stale: 'Stale', degraded: 'Degraded', failed: 'Failed',
}

const objectOf = (value: unknown): Record<string, unknown> => value && typeof value === 'object' ? value as Record<string, unknown> : {}
const text = (value: unknown): string => String(value ?? '').trim()
const count = (value: unknown): number => {
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0
}

export function normalizeCodeGraphStatus(value: unknown): CodeGraphStatus | undefined {
  const item = objectOf(value)
  if (!Object.keys(item).length) return undefined
  const mode = text(item.mode).toLowerCase()
  const rawState = text(item.state).toLowerCase()
  const pending = objectOf(item.pending_changes ?? item.pendingChanges)
  return {
    mode,
    state: STATES.has(rawState) ? rawState : mode === 'off' ? 'off' : 'degraded',
    version: text(item.version),
    initialized: item.initialized === true,
    freshness: text(item.freshness).toLowerCase(),
    lastIndexed: text(item.last_indexed ?? item.lastIndexed),
    fileCount: count(item.file_count ?? item.fileCount),
    nodeCount: count(item.node_count ?? item.nodeCount),
    edgeCount: count(item.edge_count ?? item.edgeCount),
    pendingChanges: {
      added: count(pending.added), modified: count(pending.modified), removed: count(pending.removed),
    },
    indexState: text(item.index_state ?? item.indexState),
    pendingRefs: count(item.pending_refs ?? item.pendingRefs),
    reindexRecommended: (item.reindex_recommended ?? item.reindexRecommended) === true,
    worktreeMismatch: (item.worktree_mismatch ?? item.worktreeMismatch) === true,
    lastError: text(item.last_error ?? item.lastError),
  }
}

export const codegraphStatusLabel = (status: CodeGraphStatus | undefined): string => status ? LABELS[status.state] ?? 'Degraded' : ''

export function codegraphStatusSummary(status: CodeGraphStatus | undefined): string {
  if (!status) return ''
  const parts = [codegraphStatusLabel(status)]
  if (status.version) parts.push(`v${status.version}`)
  const pending = status.pendingChanges.added + status.pendingChanges.modified + status.pendingChanges.removed
  if (status.state === 'ready' && status.nodeCount > 0) parts.push(`${status.nodeCount} nodes`)
  if (pending > 0) parts.push(`${pending} files pending`)
  if (status.state === 'degraded') {
    if (status.worktreeMismatch) parts.push('worktree mismatch')
    else if (status.reindexRecommended) parts.push('reindex recommended')
    else if (status.pendingRefs > 0) parts.push(`${status.pendingRefs} pending refs`)
    else if (status.indexState && status.indexState !== 'complete') parts.push(`${status.indexState} index`)
    else parts.push('status unavailable')
  }
  return parts.join(' · ')
}

export function codegraphStatusColor(status: CodeGraphStatus | undefined): string {
  if (!status) return '#888888'
  if (status.state === 'ready') return '#00d2ff'
  if (['initializing', 'syncing', 'stale', 'degraded'].includes(status.state)) return '#f7c948'
  if (status.state === 'failed') return '#ff5d5d'
  return '#888888'
}
