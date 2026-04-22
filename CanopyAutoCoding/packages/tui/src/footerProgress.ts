import type { WorkerSnapshot } from './types'

export const FOOTER_SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'] as const

type FooterProgressContext = {
  status: string
  route: string
  activeStage: string
  activeStageLabel: string
  requirementsWorkers: WorkerSnapshot[]
  reviewWorkers: WorkerSnapshot[]
  designWorkers: WorkerSnapshot[]
  taskSplitWorkers: WorkerSnapshot[]
  developmentWorkers: WorkerSnapshot[]
}

const STARTUP_PROGRESS_PATTERNS = [
  '智能体启动中',
  '启动审核器',
  '初始化审核器',
  '配置审核器',
  '配置审核器模型',
] as const

function isBusyWorker(worker: WorkerSnapshot): boolean {
  const agentState = String(worker.agentState || '').trim().toUpperCase()
  const status = String(worker.status || '').trim().toLowerCase()
  const runtimeStatus = String(worker.currentTaskRuntimeStatus || '').trim().toLowerCase()
  return agentState === 'BUSY' || status === 'running' || runtimeStatus === 'running'
}

function inferStageKey(route: string, activeStage: string): string {
  const normalizedStage = String(activeStage || '').trim()
  if (normalizedStage === 'stage.a02.start') return 'requirements-intake'
  if (normalizedStage === 'stage.a03.start') return 'requirements-clarification'
  if (normalizedStage === 'stage.a04.start') return 'review'
  if (normalizedStage === 'stage.a05.start') return 'design'
  if (normalizedStage === 'stage.a06.start') return 'task-split'
  if (normalizedStage === 'stage.a07.start') return 'development'
  return String(route || '').trim()
}

function stageWorkers(context: FooterProgressContext): WorkerSnapshot[] {
  switch (inferStageKey(context.route, context.activeStage)) {
    case 'requirements-intake':
    case 'requirements-clarification':
    case 'requirements':
      return context.requirementsWorkers
    case 'review':
      return context.reviewWorkers
    case 'design':
      return context.designWorkers
    case 'task-split':
      return context.taskSplitWorkers
    case 'development':
      return context.developmentWorkers
    default:
      return []
  }
}

function stageBusyLabel(context: FooterProgressContext): string {
  switch (inferStageKey(context.route, context.activeStage)) {
    case 'requirements-intake':
      return '需求录入 / 执行中'
    case 'requirements-clarification':
      return '需求澄清 / 执行中'
    case 'review':
      return '需求评审 / 审核中'
    case 'design':
      return '详细设计 / 审核中'
    case 'task-split':
      return '任务拆分 / 审核中'
    case 'development':
      return '任务开发 / 执行中'
    default:
      return `${context.activeStageLabel || '当前阶段'} / 执行中`
  }
}

export function resolveFooterProgressLine(
  context: FooterProgressContext,
  explicitProgressLine: string,
  frameIndex: number,
): string {
  const explicit = String(explicitProgressLine || '').trim()
  const normalizedStatus = String(context.status || '').trim().toLowerCase()
  if (normalizedStatus !== 'running') return explicit
  const busyWorkers = stageWorkers(context).filter(isBusyWorker)
  const busyCount = busyWorkers.length
  if (explicit) {
    const looksLikeStartup = STARTUP_PROGRESS_PATTERNS.some((pattern) => explicit.includes(pattern))
    if (!looksLikeStartup || busyCount === 0) return explicit
  }
  if (busyCount === 0) return explicit
  const spinner = FOOTER_SPINNER_FRAMES[Math.abs(frameIndex) % FOOTER_SPINNER_FRAMES.length] || FOOTER_SPINNER_FRAMES[0]
  const suffix = busyCount > 1 ? ` | ${busyCount} 个智能体执行中` : ''
  return `${spinner} ${stageBusyLabel(context)}${suffix}`
}
