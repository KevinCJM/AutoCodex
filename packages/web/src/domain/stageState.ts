import type { AppSnapshot, StageFailureSnapshot } from './types'

export type StageGenerationCursor = {
  action: string
  stageSeq: number
  runnerId: string
  terminalStatus: string
  terminalStageSeq: number
  terminalRunnerId: string
}

export const EMPTY_STAGE_GENERATION: StageGenerationCursor = {
  action: '',
  stageSeq: 0,
  runnerId: '',
  terminalStatus: '',
  terminalStageSeq: 0,
  terminalRunnerId: '',
}

type StagePayload = Record<string, unknown>

function normalizeSeq(value: unknown): number {
  const next = Number(value)
  return Number.isFinite(next) && next > 0 ? Math.floor(next) : 0
}

function terminal(status: string): boolean {
  return status === 'failed' || status === 'error' || status === 'completed'
}

function failure(status: string): boolean {
  return status === 'failed' || status === 'error'
}

function shellQuote(value: string): string {
  if (value && !/[\s'"`$;&|<>(){}\[\]*?!\\]/u.test(value)) return value
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

export function applyStageGeneration(cursor: StageGenerationCursor, payload: StagePayload): {
  cursor: StageGenerationCursor
  accepted: boolean
  action: string
  status: string
  stageSeq: number
  runnerId: string
  source: string
  authoritativeFailure: boolean
} {
  const action = String(payload.action ?? '').trim()
  const status = String(payload.status ?? 'running').trim().toLowerCase() || 'running'
  const stageSeq = normalizeSeq(payload.stage_seq ?? payload.stageSeq)
  const runnerId = String(payload.runner_id ?? payload.runnerId ?? '').trim()
  const source = String(payload.source ?? '').trim().toLowerCase()
  const result = (accepted: boolean, next = cursor) => ({
    cursor: next,
    accepted,
    action,
    status,
    stageSeq,
    runnerId,
    source,
    authoritativeFailure: accepted && source === 'runner_failure' && failure(status),
  })

  if (stageSeq > 0 && cursor.stageSeq > 0 && stageSeq < cursor.stageSeq) return result(false)
  if (stageSeq === cursor.stageSeq && runnerId && cursor.runnerId && runnerId !== cursor.runnerId) return result(false)
  if (failure(cursor.terminalStatus)) {
    const newRunner = source === 'runner_start'
      && Boolean(runnerId)
      && runnerId !== cursor.terminalRunnerId
      && (!stageSeq || !cursor.terminalStageSeq || stageSeq > cursor.terminalStageSeq)
    const repeatedFailure = source === 'runner_failure'
      && failure(status)
      && (!runnerId || !cursor.terminalRunnerId || runnerId === cursor.terminalRunnerId)
      && (!stageSeq || !cursor.terminalStageSeq || stageSeq === cursor.terminalStageSeq)
    if (!newRunner && !repeatedFailure) return result(false)
  }
  const sameGeneration = (!stageSeq || !cursor.stageSeq || stageSeq === cursor.stageSeq)
    && (!runnerId || !cursor.runnerId || runnerId === cursor.runnerId)
    && (!action || !cursor.action || action === cursor.action)
  if (sameGeneration && cursor.terminalStatus && !terminal(status)) return result(false)
  if (sameGeneration && cursor.runnerId && !runnerId && !source.startsWith('runner_')) return result(false)

  const next: StageGenerationCursor = {
    action: action || cursor.action,
    stageSeq: stageSeq || cursor.stageSeq,
    runnerId: runnerId || cursor.runnerId,
    terminalStatus: cursor.terminalStatus,
    terminalStageSeq: cursor.terminalStageSeq,
    terminalRunnerId: cursor.terminalRunnerId,
  }
  if (source === 'runner_start' && (
    (runnerId && runnerId !== cursor.terminalRunnerId)
    || (!failure(cursor.terminalStatus) && stageSeq > cursor.terminalStageSeq)
  )) {
    next.terminalStatus = ''
    next.terminalStageSeq = 0
    next.terminalRunnerId = ''
  }
  if (terminal(status)) {
    next.terminalStatus = status
    next.terminalStageSeq = stageSeq || next.stageSeq
    next.terminalRunnerId = runnerId || next.runnerId
  }
  return result(true, next)
}

export function stagePayloadFromApp(app: AppSnapshot): StagePayload | null {
  if (app.activeStageFailure) return {
    action: app.activeStageFailure.action || app.activeStage,
    status: app.activeStageFailure.status,
    source: app.activeStageFailure.source,
    runner_id: app.activeStageFailure.runnerId,
    stage_seq: app.activeStageFailure.stageSeq,
  }
  if (!app.activeStageStatus && !app.activeStageSeq && !app.activeStageRunnerId && !app.activeStageSource) return null
  return {
    action: app.activeStage,
    status: app.activeStageStatus,
    source: app.activeStageSource,
    runner_id: app.activeStageRunnerId,
    stage_seq: app.activeStageSeq,
  }
}

export function stageFailureFromEvent(payload: StagePayload): StageFailureSnapshot | null {
  const source = String(payload.source ?? '').trim().toLowerCase()
  const status = String(payload.status ?? '').trim().toLowerCase()
  if (source !== 'runner_failure' || !failure(status)) return null
  const workers = Array.isArray(payload.orphaned_workers) ? payload.orphaned_workers : []
  return {
    action: String(payload.action ?? '').trim(),
    stageLabel: String(payload.stage_label ?? '').trim(),
    status,
    source,
    runnerId: String(payload.runner_id ?? '').trim(),
    stageSeq: normalizeSeq(payload.stage_seq),
    message: String(payload.message ?? payload.error ?? '').trim(),
    failurePath: String(payload.failure_path ?? '').trim(),
    failureKind: String(payload.failure_kind ?? '').trim(),
    orphanedWorkers: workers.map((value) => {
      const item = value && typeof value === 'object' ? value as Record<string, unknown> : {}
      const sessionName = typeof value === 'string' ? value : String(item.session_name ?? '').trim()
      const rawAttach = item.attach_command
      const attachCommand = Array.isArray(rawAttach)
        ? rawAttach.map((part) => shellQuote(String(part))).join(' ')
        : String(rawAttach ?? '').trim() || (sessionName ? `tmux attach -t ${shellQuote(sessionName)}` : '')
      return {
        workerId: String(item.worker_id ?? '').trim(),
        sessionName,
        attachCommand,
        workDir: String(item.work_dir ?? '').trim(),
      }
    }).filter((worker) => worker.sessionName || worker.attachCommand),
  }
}
