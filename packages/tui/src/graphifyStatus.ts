import type { GraphifyStatus } from './types'

const GRAPHIFY_STATES = new Set([
  'off',
  'unavailable',
  'building',
  'ready',
  'stale',
  'degraded',
  'failed',
])

const GRAPHIFY_LABELS: Record<string, string> = {
  off: 'Off',
  unavailable: 'Unavailable',
  building: 'Building',
  ready: 'Ready',
  stale: 'Stale',
  degraded: 'Degraded',
  failed: 'Failed',
}

const GRAPHIFY_QUERY_COMMANDS = new Set(['query', 'affected', 'path', 'explain', 'god-nodes'])
const GRAPHIFY_QUERY_STATUSES = new Set(['ok', 'error', 'failed', 'timeout', 'unavailable'])
const GRAPHIFY_FRESHNESS = new Set(['fresh', 'stale', 'unknown', 'cache_fallback', 'degraded'])

function objectOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function text(value: unknown): string {
  return String(value ?? '').trim()
}

function count(value: unknown): number {
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0
}

function allowedText(value: unknown, allowed: Set<string>): string {
  const normalized = text(value).toLowerCase()
  return allowed.has(normalized) ? normalized : ''
}

function timestamp(value: unknown): string {
  const normalized = text(value)
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.test(normalized)
    ? normalized
    : ''
}

export function normalizeGraphifyStatus(value: unknown): GraphifyStatus | undefined {
  const item = objectOf(value)
  if (Object.keys(item).length === 0) return undefined
  const rawState = text(item.state).toLowerCase()
  const mode = text(item.mode).toLowerCase()
  const state = GRAPHIFY_STATES.has(rawState)
    ? rawState
    : mode === 'off' ? 'off' : 'degraded'
  return {
    mode,
    state,
    version: text(item.version),
    freshness: text(item.freshness).toLowerCase(),
    generatedAt: text(item.generated_at ?? item.generatedAt),
    sourceFingerprint: text(item.source_fingerprint ?? item.sourceFingerprint),
    nodeCount: count(item.node_count ?? item.nodeCount),
    edgeCount: count(item.edge_count ?? item.edgeCount),
    evidenceId: text(item.evidence_id ?? item.evidenceId),
    directCount: count(item.direct_count ?? item.directCount),
    inferredCount: count(item.inferred_count ?? item.inferredCount),
    reportPath: text(item.report_path ?? item.reportPath),
    lastError: text(item.last_error ?? item.lastError),
    queryCountStage: count(item.query_count_stage ?? item.queryCountStage),
    lastQueryCommand: allowedText(item.last_query_command ?? item.lastQueryCommand, GRAPHIFY_QUERY_COMMANDS),
    lastQueryAt: timestamp(item.last_query_at ?? item.lastQueryAt),
    lastQueryStatus: allowedText(item.last_query_status ?? item.lastQueryStatus, GRAPHIFY_QUERY_STATUSES),
    lastQueryFreshness: allowedText(item.last_query_freshness ?? item.lastQueryFreshness, GRAPHIFY_FRESHNESS),
    lastQueryTruncated: (item.last_query_truncated ?? item.lastQueryTruncated) === true,
  }
}

export function graphifyStatusLabel(status: GraphifyStatus | undefined): string {
  if (!status) return ''
  return GRAPHIFY_LABELS[status.state] ?? 'Degraded'
}

export function graphifyStatusSummary(status: GraphifyStatus | undefined): string {
  if (!status) return ''
  const parts = [graphifyStatusLabel(status)]
  if (status.version) parts.push(`v${status.version}`)
  if (status.queryCountStage > 0) {
    parts.push(`本阶段 ${status.queryCountStage} 次查询`)
    if (status.lastQueryCommand || status.lastQueryFreshness) {
      parts.push(`最近 ${status.lastQueryCommand || 'unknown'}/${status.lastQueryFreshness || 'unknown'}`)
    }
  }
  return parts.join(' · ')
}

export function graphifyStatusColor(status: GraphifyStatus | undefined): string {
  if (!status) return '#888888'
  if (status.state === 'ready') return '#00d2ff'
  if (status.state === 'building' || status.state === 'stale' || status.state === 'degraded') return '#f7c948'
  if (status.state === 'failed') return '#ff5d5d'
  return '#888888'
}
