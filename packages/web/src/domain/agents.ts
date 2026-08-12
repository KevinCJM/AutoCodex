import { stageBusyLabel, stageRouteForAction } from './stages'
import type { HomeAgentItem, PromptSnapshot, WorkerSnapshot } from './types'

const LIVE_HEALTH = new Set(['alive', 'observe_error', 'provider_auth_error'])
const RUNNING = new Set(['running', 'busy', 'submitted', 'submitting'])
const COMPLETED = new Set(['done', 'succeeded', 'completed'])
const READY = new Set(['done', 'succeeded', 'completed', 'ready', 'idle'])
const FAILED = new Set(['failed', 'stale_failed', 'error'])
const ACTIVE_TURN = new Set(['preparing', 'submitting', 'submitted', 'waiting_result', 'submission_unknown', 'running', 'pending'])
const COMPLETED_TURN = new Set(['done', 'ready', 'succeeded', 'completed', 'failed', 'error', 'stale_failed', 'orphaned'])
const STALE_MISSING_SESSION_MS = 300_000
const STARTUP_PROGRESS_PATTERNS = ['解析参数', '准备智能体', '等待 tmux', '智能体启动中', '启动审核器', '初始化审核器', '配置审核器', '配置审核器模型'] as const
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
const DISPLAY_SOURCE_ORDER: HomeAgentItem['source'][] = ['routing', 'requirements', 'review', 'design', 'task-split', 'development', 'overall-review', 'control']
const DESIGN_REVIEW_ROLE_ORDER = ['开发工程师', '测试工程师', '架构师', '审核员']
const DEVELOPMENT_REVIEW_ROLE_ORDER = ['需求分析师', '测试工程师', '审核员', '架构师']
const VENDOR_LABELS: Record<string, string> = {
  codex: 'Codex',
  claude: 'Claude',
  gemini: 'Gemini',
  opencode: 'OpenCode',
  mimo: 'MiMo Code',
  agy: 'AGY',
  deveco: 'DevEco Code',
}

function optionalBoolean(value: unknown): boolean | null {
  if (value === undefined || value === null || value === '') return null
  if (typeof value === 'boolean') return value
  const normalized = String(value).trim().toLowerCase()
  if (normalized === 'true' || normalized === '1') return true
  if (normalized === 'false' || normalized === '0') return false
  return null
}

export function workerOwnedPromptIsReady(prompt: PromptSnapshot, workers: WorkerSnapshot[]): boolean {
  if (!prompt.pending) return true
  const payload = prompt.payload
  const explicitlyReady = optionalBoolean(payload.ready_for_human ?? payload.readyForHuman)
  if (explicitlyReady === null) return true
  if (!explicitlyReady) return false
  const ownerSession = String(payload.owner_session_name ?? payload.ownerSessionName ?? '').trim()
  if (!ownerSession) return true
  const owner = workers.find((worker) => worker.sessionName.trim() === ownerSession)
  if (!owner) return false
  const ownerRevision = Number(payload.owner_state_revision ?? payload.ownerStateRevision ?? 0)
  if (Number.isSafeInteger(ownerRevision) && ownerRevision > 0 && Number(owner.stateRevision ?? 0) < ownerRevision) {
    return false
  }
  if (owner.agentState.trim().toUpperCase() !== 'READY') return false
  const ownerTurnId = String(payload.owner_turn_id ?? payload.ownerTurnId ?? '').trim()
  const workerTurnId = String(owner.currentTurnId ?? '').trim()
  if (ownerTurnId && workerTurnId && ownerTurnId !== workerTurnId) return false
  if (ACTIVE_TURN.has(normalized(owner.turnState))) return false
  if (ACTIVE_TURN.has(normalized(owner.currentTaskRuntimeStatus))) return false
  return true
}
const EFFORT_LABELS: Record<string, string> = {
  high: 'High',
  medium: 'Medium',
  low: 'Low',
  minimal: 'Minimal',
  xhigh: 'XHigh',
  max: 'Max',
}
const PONYTAIL_MODE_LABELS: Record<string, string> = {
  off: 'Off',
  lite: 'Lite',
  full: 'Full',
  ultra: 'Ultra',
}
const REQUIREMENTS_MODE_LABELS: Record<string, string> = {
  grill: 'Grill Me',
  'grill-with-docs': 'Grill with Docs',
}

function normalized(value: unknown): string {
  return String(value ?? '').trim().toLowerCase()
}

function workerFreshness(worker: WorkerSnapshot): number {
  const updatedAt = Date.parse(String(worker.updatedAt || '').trim())
  const heartbeat = Date.parse(String(worker.lastHeartbeatAt || '').trim())
  return Math.max(Number.isFinite(updatedAt) ? updatedAt : 0, Number.isFinite(heartbeat) ? heartbeat : 0)
}

function workerRevision(worker: WorkerSnapshot): number | null {
  const value = Number(worker.stateRevision)
  return Number.isSafeInteger(value) && value >= 0 ? value : null
}

function hasActiveTurnEvidence(worker: WorkerSnapshot): boolean {
  if (RUNNING.has(normalized(worker.currentTaskRuntimeStatus))) return true
  if (RUNNING.has(normalized(worker.resultStatus)) || RUNNING.has(normalized(worker.status))) return true
  if (['submitting', 'submitted'].includes(normalized(worker.dispatchState))) return true
  return Boolean(String(worker.turnStatusPath || '').trim())
}

function isStaleMissingSessionLiveWorker(worker: WorkerSnapshot): boolean {
  if (worker.sessionExists !== false || !LIVE_HEALTH.has(normalized(worker.healthStatus))) return false
  if (FAILED.has(normalized(worker.status)) || FAILED.has(normalized(worker.resultStatus))) return false
  const agentState = String(worker.agentState || '').trim().toUpperCase()
  if (agentState === 'DEAD' || agentState === 'STARTING' || hasActiveTurnEvidence(worker)) return false
  const freshness = workerFreshness(worker)
  return freshness > 0 && freshness <= Date.now() && Date.now() - freshness > STALE_MISSING_SESSION_MS
}

export function resolveAgentState(worker: WorkerSnapshot): string {
  const explicit = String(worker.agentState || '').trim().toUpperCase()
  const health = normalized(worker.healthStatus)
  const status = normalized(worker.status)
  const result = normalized(worker.resultStatus)
  const runtime = normalized(worker.currentTaskRuntimeStatus)
  if (explicit === 'DEAD') return 'DEAD'
  if (explicit === 'STARTING') return 'STARTING'
  if (explicit === 'BUSY') return 'BUSY'
  if (explicit === 'READY') return 'READY'
  if (health === 'dead') return 'DEAD'
  if (COMPLETED.has(runtime) || COMPLETED.has(result) || COMPLETED.has(status)) return 'READY'
  if (FAILED.has(result) || FAILED.has(status)) return 'READY'
  if (READY.has(runtime) || READY.has(result) || READY.has(status)) return 'READY'
  if (RUNNING.has(runtime) || RUNNING.has(result) || RUNNING.has(status)) return 'BUSY'
  return 'UNKNOWN'
}

export function isLiveWorker(worker: WorkerSnapshot): boolean {
  if (!String(worker.sessionName || '').trim() || isStaleMissingSessionLiveWorker(worker)) return false
  const state = resolveAgentState(worker)
  if (worker.sessionExists === true) return true
  if (state === 'BUSY' || state === 'DEAD' || state === 'STARTING') return true
  return LIVE_HEALTH.has(normalized(worker.healthStatus))
}

export function isBusyTurnWorker(worker: WorkerSnapshot): boolean {
  if (isStaleMissingSessionLiveWorker(worker) || resolveAgentState(worker) !== 'BUSY') return false
  const runtime = normalized(worker.currentTaskRuntimeStatus)
  const result = normalized(worker.resultStatus)
  const status = normalized(worker.status)
  const dispatch = normalized(worker.dispatchState)
  const turn = normalized(worker.turnState)
  if (COMPLETED_TURN.has(runtime)) return false
  if (ACTIVE_TURN.has(runtime)) return true
  if (COMPLETED_TURN.has(turn)) return false
  if (ACTIVE_TURN.has(turn) || ACTIVE_TURN.has(dispatch)) return true
  if (COMPLETED_TURN.has(result)) return false
  if (ACTIVE_TURN.has(result)) return true
  if (COMPLETED_TURN.has(status)) return false
  if (ACTIVE_TURN.has(status)) return true
  return true
}

export function resolveAgentProgressLine(input: {
  status: string
  action: string
  activeRunnerId: string
  stageLabel: string
  stageMessage: string
  explicitProgress: string
  workers: WorkerSnapshot[]
}): string {
  const status = normalized(input.status)
  const explicit = String(input.explicitProgress || '').trim()
  if (status !== 'running') return explicit
  const fallback = explicit || String(input.stageMessage || '').trim()
  const busyCount = input.workers.filter((worker) => {
    if (worker.workflowAction && worker.workflowAction !== input.action) return false
    if (worker.stageRunnerId && input.activeRunnerId && worker.stageRunnerId !== input.activeRunnerId) return false
    return isBusyTurnWorker(worker)
  }).length
  if (fallback) {
    const startup = STARTUP_PROGRESS_PATTERNS.some((pattern) => fallback.includes(pattern))
    if (!startup || busyCount === 0) return fallback
  }
  if (busyCount === 0) return fallback
  const suffix = busyCount > 1 ? ` | ${busyCount} 个智能体执行中` : ''
  return `${stageBusyLabel(input.action, input.stageLabel)}${suffix}`
}

function titleCase(value: string): string {
  const text = String(value || '').trim()
  return text ? text.slice(0, 1).toUpperCase() + text.slice(1) : ''
}

export function buildAgentConfigLabel(worker: WorkerSnapshot): string {
  const vendorId = normalized(worker.vendor)
  const vendor = VENDOR_LABELS[vendorId] || titleCase(vendorId)
  const rawModel = String(worker.model || worker.resolvedModel || '').trim()
  const model = rawModel.toLowerCase().startsWith('gpt-') ? `GPT-${rawModel.slice(4)}` : rawModel
  const effortId = normalized(worker.reasoningEffort)
  const effort = EFFORT_LABELS[effortId] || titleCase(effortId)
  const modelAndEffort = [model, effort].filter(Boolean).join(', ')
  const ponytailMode = normalized(worker.ponytailMode)
  const ponytail = ponytailMode
    ? `Ponytail ${PONYTAIL_MODE_LABELS[ponytailMode] || titleCase(ponytailMode)}`
    : ''
  const requirementsMode = normalized(worker.requirementsMode).replaceAll('_', '-')
  const requirementsBehavior = normalized(worker.requirementsBehavior)
  const requirementsLabel = requirementsMode && requirementsMode !== 'standard'
    ? REQUIREMENTS_MODE_LABELS[requirementsMode] || `Requirements ${titleCase(requirementsMode)}`
    : ''
  const requirements = requirementsLabel && requirementsBehavior === 'standard'
    ? `${requirementsLabel} · 已完成`
    : requirementsLabel
  return [vendor, modelAndEffort, ponytail, requirements].filter(Boolean).join(' | ')
}

function effectiveSource(
  fallback: HomeAgentItem['source'],
  worker: WorkerSnapshot,
  activeStage: string,
  activeRunnerId: string,
): HomeAgentItem['source'] | null {
  const activeRoute = stageRouteForAction(activeStage)
  if (!activeRoute) return fallback
  const action = String(worker.workflowAction || '').trim()
  const runnerId = String(worker.stageRunnerId || '').trim()
  if (action && action !== activeStage) return null
  if (runnerId && activeRunnerId && runnerId !== activeRunnerId) return null
  if (action === activeStage && runnerId && activeRunnerId && runnerId === activeRunnerId) return activeRoute
  return fallback === 'control' || fallback === activeRoute ? fallback : null
}

function shouldReplace(previous: WorkerSnapshot, next: WorkerSnapshot, previousSource: HomeAgentItem['source'], nextSource: HomeAgentItem['source']): boolean {
  const previousPath = String(previous.statePath || '').trim()
  const nextPath = String(next.statePath || '').trim()
  const sameStateStream = (previousPath && nextPath && previousPath === nextPath) || (!previousPath && !nextPath)
  const previousRevision = workerRevision(previous)
  const nextRevision = workerRevision(next)
  if (sameStateStream && previousRevision !== null && nextRevision !== null && previousRevision !== nextRevision) {
    return nextRevision > previousRevision
  }
  const previousFreshness = workerFreshness(previous)
  const nextFreshness = workerFreshness(next)
  if (nextFreshness !== previousFreshness) return nextFreshness > previousFreshness
  const previousStateRank = ['UNKNOWN', 'DEAD', 'READY', 'STARTING', 'BUSY'].indexOf(resolveAgentState(previous))
  const nextStateRank = ['UNKNOWN', 'DEAD', 'READY', 'STARTING', 'BUSY'].indexOf(resolveAgentState(next))
  if (nextStateRank !== previousStateRank) return nextStateRank > previousStateRank
  return SOURCE_RANK[nextSource] > SOURCE_RANK[previousSource]
}

function sessionRole(sessionName: string): string {
  const value = String(sessionName || '').trim()
  const separator = value.indexOf('-')
  return (separator >= 0 ? value.slice(0, separator) : value).trim()
}

function roleRank(source: HomeAgentItem['source'], worker: WorkerSnapshot): number {
  const workerId = normalized(worker.workerId)
  const role = sessionRole(worker.sessionName)
  if (source === 'routing') return workerId === 'routing-initializer' || role === '路由器' ? 0 : 100
  if (source === 'requirements') {
    return workerId === 'requirements-notion-reader' || workerId === 'requirements-analyst' || ['需求录入员', '需求分析师', '分析师'].includes(role) ? 0 : 100
  }
  if (source === 'review') {
    if (workerId === 'requirements-review-analyst' || ['评审分析师', '需求分析师'].includes(role)) return 0
    const match = workerId.match(/^requirements-review-r(\d+)$/)
    if (match) return 10 + Number.parseInt(match[1] || '0', 10)
    return role === '审核器' || role === '审核员' ? 50 : 100
  }
  if (source === 'design' || source === 'task-split') {
    if (workerId === 'detailed-design-analyst' || workerId === 'task-split-analyst' || ['需求分析师', '分析师'].includes(role)) return 0
    const prefix = source === 'design' ? 'detailed-design-review-' : 'task-split-review-'
    const roleName = workerId.startsWith(prefix) ? String(worker.workerId || '').slice(prefix.length).trim() : role
    const index = DESIGN_REVIEW_ROLE_ORDER.indexOf(roleName)
    return 10 + (index >= 0 ? index : DESIGN_REVIEW_ROLE_ORDER.length)
  }
  if (source === 'development' || source === 'overall-review') {
    if (workerId === 'development-developer' || role === '开发工程师') return 0
    const roleName = workerId.startsWith('development-review-') ? String(worker.workerId || '').slice('development-review-'.length).trim() : role
    const index = DEVELOPMENT_REVIEW_ROLE_ORDER.indexOf(roleName)
    return 10 + (index >= 0 ? index : DEVELOPMENT_REVIEW_ROLE_ORDER.length)
  }
  return 100
}

function sourceDisplayRank(source: HomeAgentItem['source'], activeRoute: string): number {
  if (activeRoute) {
    if (source === activeRoute) return 0
    if (source === 'control') return 1
    return 2 + DISPLAY_SOURCE_ORDER.indexOf(source)
  }
  const index = DISPLAY_SOURCE_ORDER.indexOf(source)
  return index >= 0 ? index : DISPLAY_SOURCE_ORDER.length
}

export function buildHomeAgents(
  sources: Array<{ source: HomeAgentItem['source']; workers: WorkerSnapshot[] }>,
  activeStage = '',
  activeRunnerId = '',
): HomeAgentItem[] {
  const selected = new Map<string, { worker: WorkerSnapshot; source: HomeAgentItem['source'] }>()
  for (const collection of sources) {
    for (const worker of collection.workers) {
      const source = effectiveSource(collection.source, worker, activeStage, activeRunnerId)
      if (!source || !isLiveWorker(worker)) continue
      const sessionName = String(worker.sessionName || '').trim()
      if (!sessionName) continue
      const previous = selected.get(sessionName)
      if (previous && !shouldReplace(previous.worker, worker, previous.source, source)) continue
      selected.set(sessionName, { worker, source })
    }
  }
  const activeRoute = stageRouteForAction(activeStage)
  return [...selected.values()]
    .sort((left, right) => {
      const sourceRank = sourceDisplayRank(left.source, activeRoute) - sourceDisplayRank(right.source, activeRoute)
      if (sourceRank !== 0) return sourceRank
      const workerRoleRank = roleRank(left.source, left.worker) - roleRank(right.source, right.worker)
      return workerRoleRank || left.worker.sessionName.localeCompare(right.worker.sessionName, 'zh-CN')
    })
    .map(({ worker, source }) => ({
      source,
      workerId: String(worker.workerId || '').trim(),
      sessionName: String(worker.sessionName || '').trim(),
      healthStatus: String(worker.healthStatus || 'unknown').trim() || 'unknown',
      agentState: resolveAgentState(worker),
      turnState: String(worker.turnState || worker.currentTaskRuntimeStatus || '').trim(),
      agentConfigLabel: buildAgentConfigLabel(worker),
      attachCommand: `tmux attach -t ${worker.sessionName}`,
      workDir: worker.workDir,
    }))
}

export function reconcileWorkerSnapshots(previous: WorkerSnapshot[], incoming: WorkerSnapshot[]): WorkerSnapshot[] {
  const previousBySession = new Map(previous.map((worker) => [String(worker.sessionName || '').trim(), worker]))
  return incoming.map((worker) => {
    const old = previousBySession.get(String(worker.sessionName || '').trim())
    if (!old) return worker
    const oldPath = String(old.statePath || '').trim()
    const nextPath = String(worker.statePath || '').trim()
    const sameStream = (oldPath && nextPath && oldPath === nextPath) || (!oldPath && !nextPath)
    const oldRevision = workerRevision(old)
    const nextRevision = workerRevision(worker)
    if (sameStream && oldRevision !== null && nextRevision !== null) return oldRevision > nextRevision ? old : worker
    return workerFreshness(old) > workerFreshness(worker) ? old : worker
  })
}
