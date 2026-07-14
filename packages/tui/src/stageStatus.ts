export type StageCursor = {
  activeAction: string
  activeStageSeq: number
  activeRunnerId: string
  activeSource: string
  terminalAction: string
  terminalStageSeq: number
  terminalRunnerId: string
  terminalStatus: string
}

export const EMPTY_STAGE_CURSOR: StageCursor = {
  activeAction: '',
  activeStageSeq: 0,
  activeRunnerId: '',
  activeSource: '',
  terminalAction: '',
  terminalStageSeq: 0,
  terminalRunnerId: '',
  terminalStatus: '',
}

export function normalizeStageSeq(value: unknown): number {
  const candidate = Number(value)
  if (!Number.isFinite(candidate) || candidate <= 0) return 0
  return Math.floor(candidate)
}

export function isTerminalStageStatus(status: string): boolean {
  return ['failed', 'error', 'completed'].includes(String(status ?? '').trim().toLowerCase())
}

export function isAuthoritativeRunnerSource(source: string): boolean {
  return ['runner_start', 'runner_complete', 'runner_failure'].includes(String(source ?? '').trim().toLowerCase())
}

type StageChangedPayload = {
  action?: unknown
  status?: unknown
  source?: unknown
  runner_id?: unknown
  runnerId?: unknown
  stage_seq?: unknown
  stageSeq?: unknown
}

type ProgressPayload = {
  action?: unknown
  runner_id?: unknown
  runnerId?: unknown
  stage_seq?: unknown
  stageSeq?: unknown
}

function isFailureStatus(status: string): boolean {
  return status === 'failed' || status === 'error'
}

function isSameGeneration(cursor: StageCursor, action: string, stageSeq: number, runnerId: string): boolean {
  if (stageSeq > 0 && cursor.activeStageSeq > 0 && stageSeq !== cursor.activeStageSeq) return false
  if (runnerId && cursor.activeRunnerId && runnerId !== cursor.activeRunnerId) return false
  return !action || !cursor.activeAction || action === cursor.activeAction
}

function failureGenerationCanBeReplaced(
  cursor: StageCursor,
  source: string,
  stageSeq: number,
  runnerId: string,
): boolean {
  if (!isFailureStatus(cursor.terminalStatus)) return true
  if (source !== 'runner_start' || !runnerId) return false
  if (cursor.terminalRunnerId && runnerId === cursor.terminalRunnerId) return false
  if (stageSeq > 0 && cursor.terminalStageSeq > 0 && stageSeq <= cursor.terminalStageSeq) return false
  return true
}

export function applyStageChanged(cursor: StageCursor, payload: StageChangedPayload): {
  cursor: StageCursor
  accepted: boolean
  status: string
  source: string
  runnerId: string
  stageSeq: number
  authoritativeFailure: boolean
} {
  const action = String(payload.action ?? '').trim()
  const status = String(payload.status ?? 'running').trim().toLowerCase() || 'running'
  const source = String(payload.source ?? '').trim().toLowerCase()
  const runnerId = String(payload.runner_id ?? payload.runnerId ?? '').trim()
  const stageSeq = normalizeStageSeq(payload.stage_seq ?? payload.stageSeq)
  const rejected = () => ({
    cursor,
    accepted: false,
    status,
    source,
    runnerId,
    stageSeq,
    authoritativeFailure: false,
  })

  if (stageSeq > 0 && cursor.activeStageSeq > 0 && stageSeq < cursor.activeStageSeq) return rejected()
  if (
    stageSeq > 0
    && stageSeq === cursor.activeStageSeq
    && runnerId
    && cursor.activeRunnerId
    && runnerId !== cursor.activeRunnerId
  ) return rejected()

  if (!failureGenerationCanBeReplaced(cursor, source, stageSeq, runnerId)) {
    if (!isSameGeneration(cursor, action, stageSeq, runnerId)) return rejected()
    if (!(source === 'runner_failure' && isFailureStatus(status))) return rejected()
  }

  const sameGeneration = isSameGeneration(cursor, action, stageSeq, runnerId)
  if (
    sameGeneration
    && cursor.terminalStatus
    && !isTerminalStageStatus(status)
  ) return rejected()

  // Once a runner-scoped generation is visible, unscoped runtime inference cannot
  // move that same generation. This keeps worker health separate from runner truth.
  if (
    sameGeneration
    && cursor.activeRunnerId
    && !runnerId
    && !isAuthoritativeRunnerSource(source)
  ) return rejected()

  const next: StageCursor = {
    activeAction: action || cursor.activeAction,
    activeStageSeq: stageSeq || cursor.activeStageSeq,
    activeRunnerId: runnerId || cursor.activeRunnerId,
    activeSource: source || cursor.activeSource,
    terminalAction: cursor.terminalAction,
    terminalStageSeq: cursor.terminalStageSeq,
    terminalRunnerId: cursor.terminalRunnerId,
    terminalStatus: cursor.terminalStatus,
  }

  const startsReplacementGeneration = source === 'runner_start' && (
    (runnerId && runnerId !== cursor.terminalRunnerId)
    || (stageSeq > 0 && stageSeq > cursor.terminalStageSeq && !isFailureStatus(cursor.terminalStatus))
  )
  if (startsReplacementGeneration) {
    next.terminalAction = ''
    next.terminalStageSeq = 0
    next.terminalRunnerId = ''
    next.terminalStatus = ''
  }

  if (isTerminalStageStatus(status)) {
    next.terminalAction = action || next.activeAction
    next.terminalStageSeq = stageSeq || next.activeStageSeq
    next.terminalRunnerId = runnerId || next.activeRunnerId
    next.terminalStatus = status
  }

  return {
    cursor: next,
    accepted: true,
    status,
    source,
    runnerId,
    stageSeq,
    authoritativeFailure: source === 'runner_failure' && isFailureStatus(status),
  }
}

function getObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

export function inferBootstrapStatus(payload: Record<string, unknown>): string {
  const snapshots = getObject(payload.snapshots)
  const app = getObject(snapshots.app)
  const failure = getObject(app.active_stage_failure ?? app.activeStageFailure)
  const failureSource = String(failure.source ?? '').trim().toLowerCase()
  const failureStatus = String(failure.status ?? '').trim().toLowerCase()
  if (failureSource === 'runner_failure' || isFailureStatus(failureStatus)) return failureStatus || 'failed'
  const activeStageStatus = String(app.active_stage_status ?? app.activeStageStatus ?? '').trim().toLowerCase()
  if (isFailureStatus(activeStageStatus)) return activeStageStatus
  if (Boolean(app.pending_hitl ?? app.pendingHitl)) return 'awaiting-input'
  if (Boolean(app.pending_attention ?? app.pendingAttention)) return 'awaiting-input'
  if (activeStageStatus === 'awaiting-input') return 'awaiting-input'
  return activeStageStatus || 'ready'
}

export function shouldAcceptProgressEvent(cursor: StageCursor, payload: ProgressPayload): boolean {
  const action = String(payload.action ?? '').trim()
  const runnerId = String(payload.runner_id ?? payload.runnerId ?? '').trim()
  const stageSeq = normalizeStageSeq(payload.stage_seq ?? payload.stageSeq)
  if (!action || stageSeq === 0) return false
  if (!cursor.activeAction || cursor.activeStageSeq === 0) return false
  if (action !== cursor.activeAction || stageSeq !== cursor.activeStageSeq) return false
  if (runnerId && cursor.activeRunnerId && runnerId !== cursor.activeRunnerId) return false
  if (
    cursor.terminalStageSeq === stageSeq
    && cursor.terminalAction === action
    && (!runnerId || !cursor.terminalRunnerId || runnerId === cursor.terminalRunnerId)
  ) return false
  return true
}

export function markTerminalStage(cursor: StageCursor): StageCursor {
  if (!cursor.activeAction || cursor.activeStageSeq === 0) return cursor
  return {
    ...cursor,
    terminalAction: cursor.activeAction,
    terminalStageSeq: cursor.activeStageSeq,
    terminalRunnerId: cursor.activeRunnerId,
    terminalStatus: 'error',
  }
}

export function isNewRunnerGeneration(previous: StageCursor, current: StageCursor): boolean {
  if (!current.activeRunnerId || current.activeRunnerId === previous.activeRunnerId) return false
  if (previous.activeStageSeq > 0 && current.activeStageSeq <= previous.activeStageSeq) return false
  return current.activeSource === 'runner_start'
    || current.activeSource === 'runner_complete'
    || current.activeSource === 'runner_failure'
}
