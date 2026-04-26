export type StageRoute =
  | 'routing'
  | 'requirements'
  | 'review'
  | 'design'
  | 'task-split'
  | 'development'
  | 'overall-review'

type StageMeta = {
  route: StageRoute
  progressKey: string
  busyLabel: string
}

export const STAGE_META_BY_ACTION: Record<string, StageMeta> = {
  'control.b01.open': {
    route: 'routing',
    progressKey: 'routing',
    busyLabel: '路由初始化 / 执行中',
  },
  'stage.a01.start': {
    route: 'routing',
    progressKey: 'routing',
    busyLabel: '路由初始化 / 执行中',
  },
  'stage.a02.start': {
    route: 'requirements',
    progressKey: 'requirements-intake',
    busyLabel: '需求录入 / 执行中',
  },
  'stage.a03.start': {
    route: 'requirements',
    progressKey: 'requirements-clarification',
    busyLabel: '需求澄清 / 执行中',
  },
  'stage.a04.start': {
    route: 'review',
    progressKey: 'review',
    busyLabel: '需求评审 / 审核中',
  },
  'stage.a05.start': {
    route: 'design',
    progressKey: 'design',
    busyLabel: '详细设计 / 审核中',
  },
  'stage.a06.start': {
    route: 'task-split',
    progressKey: 'task-split',
    busyLabel: '任务拆分 / 审核中',
  },
  'stage.a07.start': {
    route: 'development',
    progressKey: 'development',
    busyLabel: '任务开发 / 执行中',
  },
  'stage.a08.start': {
    route: 'overall-review',
    progressKey: 'overall-review',
    busyLabel: '复核 / 审核中',
  },
}

export function stageRouteForAction(activeStage: string): StageRoute | '' {
  return STAGE_META_BY_ACTION[String(activeStage || '').trim()]?.route ?? ''
}

export function stageProgressKey(route: string, activeStage: string): string {
  return STAGE_META_BY_ACTION[String(activeStage || '').trim()]?.progressKey || String(route || '').trim()
}

export function stageBusyLabel(route: string, activeStage: string, fallbackLabel: string): string {
  return STAGE_META_BY_ACTION[String(activeStage || '').trim()]?.busyLabel || `${fallbackLabel || '当前阶段'} / 执行中`
}
