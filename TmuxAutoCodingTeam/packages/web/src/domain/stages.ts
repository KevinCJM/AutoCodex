import type { StageRoute } from './types'

export const STAGE_ROUTES: StageRoute[] = [
  'routing',
  'requirements',
  'review',
  'design',
  'task-split',
  'development',
  'overall-review',
]

export const STAGE_LABELS: Record<StageRoute, string> = {
  routing: '路由层',
  requirements: '需求',
  review: '评审',
  design: '详设',
  'task-split': '任务',
  development: '开发',
  'overall-review': '复核',
}

export const STAGE_META_BY_ACTION: Record<string, { route: StageRoute; label: string }> = {
  'control.b01.open': { route: 'routing', label: '路由初始化' },
  'stage.a01.start': { route: 'routing', label: '路由初始化' },
  'stage.a02.start': { route: 'requirements', label: '需求录入' },
  'stage.a03.start': { route: 'requirements', label: '需求澄清' },
  'stage.a04.start': { route: 'review', label: '需求评审' },
  'stage.a05.start': { route: 'design', label: '详细设计' },
  'stage.a06.start': { route: 'task-split', label: '任务拆分' },
  'stage.a07.start': { route: 'development', label: '任务开发' },
  'stage.a08.start': { route: 'overall-review', label: '复核' },
}

export function stageRouteForAction(action: string): StageRoute | '' {
  return STAGE_META_BY_ACTION[String(action || '').trim()]?.route ?? ''
}

export function routeLabel(route: string): string {
  return STAGE_LABELS[route as StageRoute] ?? route
}
