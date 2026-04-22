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
}

export function isRunningWorker(worker: WorkerSnapshot): boolean {
  if (!worker.sessionName.trim()) return false
  if (String(worker.agentState || '').trim().toUpperCase() === 'DEAD') return true
  if (worker.sessionExists !== undefined) return worker.sessionExists
  return LIVE_WORKER_HEALTH_STATUSES.has(worker.healthStatus)
}

export function resolveHomeAgentState(worker: WorkerSnapshot): string {
  const agentState = String(worker.agentState || '').trim().toUpperCase()
  const status = String(worker.status || '').trim().toLowerCase()
  const currentTaskRuntimeStatus = String(worker.currentTaskRuntimeStatus || '').trim().toLowerCase()
  if (agentState === 'DEAD') return 'DEAD'
  if (agentState === 'STARTING') return 'STARTING'
  if (status === 'running' || currentTaskRuntimeStatus === 'running') return 'BUSY'
  return agentState || 'UNKNOWN'
}

export function buildHomeAgents(
  sources: Array<{ source: HomeAgentItem['source']; workers: WorkerSnapshot[] }>,
): HomeAgentItem[] {
  const deduped = new Map<string, HomeAgentItem>()
  const updatedAtBySession = new Map<string, string>()
  const sourceRankBySession = new Map<string, number>()
  for (const source of sources) {
    for (const worker of source.workers) {
      if (!isRunningWorker(worker)) continue
      const sessionName = worker.sessionName.trim()
      if (!sessionName) continue
      const nextAgentState = resolveHomeAgentState(worker)
      const updatedAt = String(worker.updatedAt || '').trim()
      const previousUpdatedAt = updatedAtBySession.get(sessionName) || ''
      const sourceRank = SOURCE_RANK[source.source] || 0
      const previousSourceRank = sourceRankBySession.get(sessionName) || 0
      const previousAgentState = String(deduped.get(sessionName)?.agentState || '').trim().toUpperCase()
      if (deduped.has(sessionName)) {
        if (previousUpdatedAt > updatedAt) continue
        if (previousUpdatedAt === updatedAt) {
          if (previousAgentState === 'DEAD' && nextAgentState !== 'DEAD') continue
          if (previousAgentState !== 'DEAD' && nextAgentState === 'DEAD') {
            // Prefer the terminal DEAD snapshot when timestamps collide.
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
      updatedAtBySession.set(sessionName, updatedAt)
      sourceRankBySession.set(sessionName, sourceRank)
    }
  }
  return [...deduped.values()]
}
