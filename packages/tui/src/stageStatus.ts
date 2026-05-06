import { stageRouteForAction } from './stageRegistry'

export type StageCursor = {
  activeAction: string
  activeStageSeq: number
  terminalAction: string
  terminalStageSeq: number
}

export const EMPTY_STAGE_CURSOR: StageCursor = {
  activeAction: '',
  activeStageSeq: 0,
  terminalAction: '',
  terminalStageSeq: 0,
}

export function normalizeStageSeq(value: unknown): number {
  const candidate = Number(value)
  if (!Number.isFinite(candidate) || candidate <= 0) return 0
  return Math.floor(candidate)
}

export function isTerminalStageStatus(status: string): boolean {
  return ['failed', 'error', 'completed'].includes(String(status ?? '').trim())
}

type StageChangedPayload = {
  action?: unknown
  status?: unknown
  stage_seq?: unknown
  stageSeq?: unknown
}

export function applyStageChanged(cursor: StageCursor, payload: StageChangedPayload): {
  cursor: StageCursor
  accepted: boolean
  status: string
} {
  const action = String(payload.action ?? '').trim()
  const status = String(payload.status ?? 'running').trim() || 'running'
  const stageSeq = normalizeStageSeq(payload.stage_seq ?? payload.stageSeq)
  const isTerminal = isTerminalStageStatus(status)
  if (
    !isTerminal &&
    stageSeq > 0 &&
    cursor.terminalStageSeq > 0 &&
    action === cursor.terminalAction &&
    stageSeq <= cursor.terminalStageSeq
  ) {
    return { cursor, accepted: false, status }
  }
  const next: StageCursor = {
    activeAction: action || cursor.activeAction,
    activeStageSeq: stageSeq || cursor.activeStageSeq,
    terminalAction: cursor.terminalAction,
    terminalStageSeq: cursor.terminalStageSeq,
  }
  if (isTerminal) {
    next.terminalAction = action || next.activeAction
    next.terminalStageSeq = stageSeq || next.activeStageSeq
    return { cursor: next, accepted: true, status }
  }
  if (
    stageSeq > 0 &&
    (
      next.terminalStageSeq === 0
      || stageSeq > next.terminalStageSeq
      || action !== next.terminalAction
    )
  ) {
    next.terminalAction = ''
    next.terminalStageSeq = 0
  }
  return { cursor: next, accepted: true, status }
}

type ProgressPayload = {
  action?: unknown
  stage_seq?: unknown
  stageSeq?: unknown
}

function getObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function getWorkers(value: unknown): Array<Record<string, unknown>> {
  const snapshot = getObject(value)
  const workers = snapshot.workers
  return Array.isArray(workers) ? workers.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object') : []
}

function workerHasLiveWork(worker: Record<string, unknown>): boolean {
  const sessionName = String(worker.session_name ?? worker.sessionName ?? '').trim()
  if (!sessionName) return false
  const agentState = String(worker.agent_state ?? worker.agentState ?? '').trim().toUpperCase()
  const status = String(worker.status ?? '').trim().toLowerCase()
  const runtimeStatus = String(worker.current_task_runtime_status ?? worker.currentTaskRuntimeStatus ?? '').trim().toLowerCase()
  const healthStatus = String(worker.health_status ?? worker.healthStatus ?? '').trim().toLowerCase()
  if (agentState === 'DEAD' || healthStatus === 'dead') return false
  if (agentState === 'READY') return false
  if (['done', 'succeeded', 'completed'].includes(runtimeStatus)) return false
  if (['done', 'succeeded', 'completed', 'ready', 'idle', 'failed', 'stale_failed', 'error'].includes(status)) return false
  if (agentState === 'BUSY' || agentState === 'STARTING') return true
  if (status === 'running' || status === 'pending' || runtimeStatus === 'running') return true
  return false
}

function stageSnapshotForAction(snapshots: Record<string, unknown>, activeStage: string): unknown {
  const stages = getObject(snapshots.stages)
  const route = stageRouteForAction(activeStage)
  return route ? stages[route] : undefined
}

export function stageSnapshotHasLiveWork(snapshot: unknown): boolean {
  return getWorkers(snapshot).some(workerHasLiveWork)
}

export function inferBootstrapStatus(payload: Record<string, unknown>): string {
  const snapshots = getObject(payload.snapshots)
  const app = getObject(snapshots.app)
  if (Boolean(app.pending_hitl ?? app.pendingHitl)) return 'awaiting-input'
  const activeStage = String(app.active_stage ?? app.activeStage ?? '').trim()
  const stageSnapshot = stageSnapshotForAction(snapshots, activeStage)
  if (getWorkers(stageSnapshot).some(workerHasLiveWork)) return 'running'
  return 'ready'
}

export function shouldRecoverRunningFromStageSnapshot(
  currentStatus: string,
  activeStage: string,
  route: string,
  snapshot: unknown,
  hasPendingInput = false,
): boolean {
  const normalizedStatus = String(currentStatus ?? '').trim().toLowerCase()
  if (
    normalizedStatus !== 'failed' &&
    normalizedStatus !== 'error' &&
    normalizedStatus !== 'awaiting-input'
  ) return false
  if (normalizedStatus === 'awaiting-input' && hasPendingInput) return false
  const activeRoute = stageRouteForAction(activeStage)
  if (!activeRoute || String(route || '').trim() !== activeRoute) return false
  return stageSnapshotHasLiveWork(snapshot)
}

export function shouldAcceptProgressEvent(cursor: StageCursor, payload: ProgressPayload): boolean {
  const action = String(payload.action ?? '').trim()
  const stageSeq = normalizeStageSeq(payload.stage_seq ?? payload.stageSeq)
  if (!action || stageSeq === 0) return false
  if (
    cursor.terminalStageSeq > 0 &&
    action === cursor.terminalAction &&
    stageSeq <= cursor.terminalStageSeq
  ) {
    return false
  }
  if (!cursor.activeAction || cursor.activeStageSeq === 0) return false
  return action === cursor.activeAction && stageSeq === cursor.activeStageSeq
}

export function markTerminalStage(cursor: StageCursor): StageCursor {
  if (!cursor.activeAction || cursor.activeStageSeq === 0) return cursor
  return {
    ...cursor,
    terminalAction: cursor.activeAction,
    terminalStageSeq: cursor.activeStageSeq,
  }
}
