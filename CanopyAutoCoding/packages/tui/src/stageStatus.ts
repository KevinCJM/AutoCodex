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
