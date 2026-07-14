import type { StageFailureSnapshot, StageFailureWorker } from './types'

function objectOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function shellQuote(value: string): string {
  if (value && !/[\s'"`$;&|<>(){}\[\]*?!\\]/u.test(value)) return value
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

function normalizeAttachCommand(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => shellQuote(String(item))).filter(Boolean).join(' ')
  return String(value ?? '').trim()
}

function normalizeFailureWorker(value: unknown): StageFailureWorker | null {
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text) return null
    if (text.startsWith('tmux ')) {
      return { workerId: '', sessionName: '', attachCommand: text, workDir: '' }
    }
    return { workerId: '', sessionName: text, attachCommand: `tmux attach -t ${shellQuote(text)}`, workDir: '' }
  }
  const worker = objectOf(value)
  const sessionName = String(worker.session_name ?? worker.sessionName ?? '').trim()
  const attachCommand = normalizeAttachCommand(worker.attach_command ?? worker.attachCommand)
    || (sessionName ? `tmux attach -t ${shellQuote(sessionName)}` : '')
  if (!sessionName && !attachCommand) return null
  return {
    workerId: String(worker.worker_id ?? worker.workerId ?? '').trim(),
    sessionName,
    attachCommand,
    workDir: String(worker.work_dir ?? worker.workDir ?? '').trim(),
  }
}

export function normalizeStageFailure(
  value: unknown,
  fallback: Partial<StageFailureSnapshot> = {},
): StageFailureSnapshot | null {
  const failure = objectOf(value)
  if (Object.keys(failure).length === 0 && Object.keys(fallback).length === 0) return null
  const rawWorkers = failure.orphaned_workers ?? failure.orphanedWorkers ?? fallback.orphanedWorkers ?? []
  const orphanedWorkers = (Array.isArray(rawWorkers) ? rawWorkers : [])
    .map(normalizeFailureWorker)
    .filter((worker): worker is StageFailureWorker => worker !== null)
    .filter((worker, index, all) => {
      const key = worker.attachCommand || worker.sessionName || worker.workerId
      return Boolean(key) && all.findIndex((candidate) => (candidate.attachCommand || candidate.sessionName || candidate.workerId) === key) === index
    })
  return {
    action: String(failure.action ?? fallback.action ?? '').trim(),
    stageLabel: String(failure.stage_label ?? failure.stageLabel ?? fallback.stageLabel ?? '').trim(),
    status: String(failure.status ?? fallback.status ?? 'failed').trim().toLowerCase() || 'failed',
    source: String(failure.source ?? fallback.source ?? '').trim().toLowerCase(),
    runnerId: String(failure.runner_id ?? failure.runnerId ?? fallback.runnerId ?? '').trim(),
    stageSeq: Number(failure.stage_seq ?? failure.stageSeq ?? fallback.stageSeq ?? 0) || 0,
    message: String(failure.message ?? failure.error ?? fallback.message ?? '').trim(),
    failurePath: String(failure.failure_path ?? failure.failurePath ?? failure.path ?? fallback.failurePath ?? '').trim(),
    failureKind: String(failure.failure_kind ?? failure.failureKind ?? failure.kind ?? fallback.failureKind ?? '').trim(),
    orphanedWorkers,
  }
}

export function normalizeAppStageFailure(payload: Record<string, unknown>): StageFailureSnapshot | null {
  const rawFailure = payload.active_stage_failure ?? payload.activeStageFailure
  if (!rawFailure || typeof rawFailure !== 'object' || Object.keys(rawFailure as Record<string, unknown>).length === 0) return null
  return normalizeStageFailure(rawFailure, {
    action: String(payload.active_stage ?? payload.activeStage ?? 'idle'),
    stageLabel: String(payload.active_stage_label ?? payload.activeStageLabel ?? ''),
    status: String(payload.active_stage_status ?? payload.activeStageStatus ?? ''),
    source: String(payload.active_stage_source ?? payload.activeStageSource ?? payload.source ?? ''),
    runnerId: String(payload.active_stage_runner_id ?? payload.activeStageRunnerId ?? payload.runner_id ?? payload.runnerId ?? ''),
    stageSeq: Number(payload.active_stage_seq ?? payload.activeStageSeq ?? 0) || 0,
  })
}

export function isAuthoritativeStageFailure(failure: StageFailureSnapshot | null | undefined): failure is StageFailureSnapshot {
  if (!failure) return false
  const status = String(failure.status || '').trim().toLowerCase()
  return failure.source === 'runner_failure' && (status === 'failed' || status === 'error')
}

export function stageFailureMatchesGeneration(
  failure: StageFailureSnapshot | null | undefined,
  generation: { activeRunnerId: string; activeStageSeq: number },
): failure is StageFailureSnapshot {
  if (!isAuthoritativeStageFailure(failure)) return false
  if (generation.activeRunnerId && failure.runnerId !== generation.activeRunnerId) return false
  if (generation.activeStageSeq > 0 && failure.stageSeq !== generation.activeStageSeq) return false
  return true
}

export function formatStageFailureReport(failure: StageFailureSnapshot): string {
  const lines = [
    '',
    '阶段执行失败',
    `阶段: ${failure.stageLabel || failure.action || '(unknown)'}`,
    `原因: ${failure.message || failure.failureKind || 'unknown error'}`,
  ]
  if (failure.failurePath) lines.push(`失败记录: ${failure.failurePath}`)
  const attachCommands = failure.orphanedWorkers.map((worker) => worker.attachCommand).filter(Boolean)
  if (attachCommands.length > 0) {
    lines.push('保留的智能体现场:')
    lines.push(...attachCommands.map((command) => `  ${command}`))
  }
  lines.push('')
  return `${lines.join('\n')}\n`
}
