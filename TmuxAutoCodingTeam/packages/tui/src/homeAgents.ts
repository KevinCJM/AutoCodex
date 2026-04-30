import { stageRouteForAction } from './stageRegistry'
import type { HomeAgentItem, WorkerSnapshot } from './types'

const LIVE_WORKER_HEALTH_STATUSES = new Set(['alive', 'auto_relaunched', 'observe_error', 'provider_auth_error'])
const SOURCE_RANK: Record<HomeAgentItem['source'], number> = {
  control: 7,
  routing: 6,
  requirements: 5,
  review: 4,
  design: 3,
  'task-split': 2,
  development: 1,
  'overall-review': 1,
}

export function isRunningWorker(worker: WorkerSnapshot): boolean {
  if (!worker.sessionName.trim()) return false
  const agentState = String(worker.agentState || '').trim().toUpperCase()
  if (agentState === 'DEAD' || agentState === 'STARTING') return true
  if (worker.sessionExists !== undefined) return worker.sessionExists
  return LIVE_WORKER_HEALTH_STATUSES.has(worker.healthStatus)
}

export function resolveHomeAgentState(worker: WorkerSnapshot): string {
  const agentState = String(worker.agentState || '').trim().toUpperCase()
  if (agentState === 'DEAD') return 'DEAD'
  if (agentState === 'STARTING') return 'STARTING'
  if (agentState === 'BUSY') return 'BUSY'
  if (agentState === 'READY') return 'READY'
  return 'UNKNOWN'
}

function workerFreshnessTs(worker: WorkerSnapshot): number {
  const updatedAtTs = Date.parse(String(worker.updatedAt || '').trim())
  const heartbeatTs = Date.parse(String(worker.lastHeartbeatAt || '').trim())
  const updatedAt = Number.isFinite(updatedAtTs) ? updatedAtTs : 0
  const heartbeat = Number.isFinite(heartbeatTs) ? heartbeatTs : 0
  return Math.max(updatedAt, heartbeat)
}

function allowedHomeSources(activeStage: string): ReadonlySet<HomeAgentItem['source']> | null {
  const stageRoute = stageRouteForAction(activeStage)
  if (!stageRoute) return null
  return new Set<HomeAgentItem['source']>(['control', stageRoute as HomeAgentItem['source']])
}

export function buildHomeAgents(
  sources: Array<{ source: HomeAgentItem['source']; workers: WorkerSnapshot[] }>,
  activeStage = '',
): HomeAgentItem[] {
  const scopedSources = allowedHomeSources(activeStage)
  const deduped = new Map<string, HomeAgentItem>()
  const freshnessBySession = new Map<string, number>()
  const sourceRankBySession = new Map<string, number>()
  for (const source of sources) {
    if (scopedSources && !scopedSources.has(source.source)) continue
    for (const worker of source.workers) {
      if (!isRunningWorker(worker)) continue
      const sessionName = worker.sessionName.trim()
      if (!sessionName) continue
      const nextAgentState = resolveHomeAgentState(worker)
      const freshness = workerFreshnessTs(worker)
      const previousFreshness = freshnessBySession.get(sessionName) ?? 0
      const sourceRank = SOURCE_RANK[source.source] || 0
      const previousSourceRank = sourceRankBySession.get(sessionName) || 0
      const previousAgentState = String(deduped.get(sessionName)?.agentState || '').trim().toUpperCase()
      if (deduped.has(sessionName)) {
        if (previousFreshness > freshness) continue
        if (previousFreshness === freshness) {
          if (previousAgentState === 'DEAD' && nextAgentState !== 'DEAD') {
            // Prefer a live backend state over a same-timestamp stale DEAD projection.
          } else if (previousAgentState !== 'DEAD' && nextAgentState === 'DEAD') {
            continue
          } else if (previousSourceRank >= sourceRank) {
            continue
          }
        }
      }
      deduped.set(sessionName, {
        source: source.source,
        sessionName,
        healthStatus: worker.healthStatus || 'unknown',
        agentState: nextAgentState,
        attachCommand: `tmux attach -t ${sessionName}`,
        workDir: worker.workDir,
      })
      freshnessBySession.set(sessionName, freshness)
      sourceRankBySession.set(sessionName, sourceRank)
    }
  }
  return [...deduped.values()]
}
