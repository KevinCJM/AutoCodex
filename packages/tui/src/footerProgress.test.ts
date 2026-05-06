import { expect, test } from 'bun:test'
import { resolveFooterProgressLine } from './footerProgress'
import type { WorkerSnapshot } from './types'

function worker(overrides: Partial<WorkerSnapshot> = {}): WorkerSnapshot {
  return {
    workDir: '',
    sessionName: 'demo',
    status: '',
    workflowStage: '',
    agentState: 'READY',
    healthStatus: 'alive',
    currentTaskRuntimeStatus: '',
    retryCount: 0,
    note: '',
    transcriptPath: '',
    turnStatusPath: '',
    questionPath: '',
    answerPath: '',
    artifactPaths: [],
    ...overrides,
  }
}

test('uses explicit progress line when it already reflects active work', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a05.start',
      activeStageLabel: '详细设计',
      routingWorkers: [],
      requirementsWorkers: [],
      reviewWorkers: [],
      designWorkers: [worker({ agentState: 'BUSY' })],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [],
    },
    '⠙ 详细设计评审第 1 轮',
    3,
  )
  expect(line).toBe('⠙ 详细设计评审第 1 轮')
})

test('replaces stale startup footer text with live design review progress', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a05.start',
      activeStageLabel: '详细设计',
      routingWorkers: [],
      requirementsWorkers: [],
      reviewWorkers: [],
      designWorkers: [
        worker({ sessionName: '开发工程师-斗木獬', agentState: 'BUSY', currentTaskRuntimeStatus: 'running' }),
        worker({ sessionName: '审核员-地异星', agentState: 'BUSY', currentTaskRuntimeStatus: 'running' }),
      ],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [],
    },
    '⠦ 智能体启动中...',
    0,
  )
  expect(line).toContain('详细设计 / 审核中')
  expect(line).toContain('2 个智能体执行中')
})

test('does not count stale busy workers whose task runtime is already done', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a04.start',
      activeStageLabel: '需求评审',
      routingWorkers: [],
      requirementsWorkers: [],
      reviewWorkers: [
        worker({ sessionName: '审核器-已完成', agentState: 'BUSY', status: 'running', currentTaskRuntimeStatus: 'done' }),
        worker({ sessionName: '审核器-运行中', agentState: 'BUSY', status: 'running', currentTaskRuntimeStatus: 'running' }),
      ],
      designWorkers: [],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [],
    },
    '⠦ 智能体启动中...',
    0,
  )
  expect(line).toContain('需求评审 / 审核中')
  expect(line).not.toContain('2 个智能体执行中')
})

test('derives busy footer text from live workers when no progress event is visible', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a05.start',
      activeStageLabel: '详细设计',
      routingWorkers: [],
      requirementsWorkers: [],
      reviewWorkers: [],
      designWorkers: [worker({ agentState: 'BUSY', status: 'running', currentTaskRuntimeStatus: 'running' })],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [],
    },
    '',
    1,
  )
  expect(line).toContain('详细设计 / 审核中')
})

test('keeps startup fallback when no live worker is actually running', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a05.start',
      activeStageLabel: '详细设计',
      routingWorkers: [],
      requirementsWorkers: [],
      reviewWorkers: [],
      designWorkers: [worker({ agentState: 'READY', status: 'ready', currentTaskRuntimeStatus: 'done' })],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [],
    },
    '⠦ 智能体启动中...',
    0,
  )
  expect(line).toBe('⠦ 智能体启动中...')
})

test('does not synthesize busy footer from worker loop when backend says READY', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a05.start',
      activeStageLabel: '详细设计',
      routingWorkers: [],
      requirementsWorkers: [],
      reviewWorkers: [],
      designWorkers: [worker({ agentState: 'READY', status: 'running', currentTaskRuntimeStatus: 'running' })],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [],
    },
    '',
    0,
  )
  expect(line).toBe('')
})

test('replaces stale startup footer text with live routing progress', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a01.start',
      activeStageLabel: '路由初始化',
      routingWorkers: [
        worker({ sessionName: '路由器-地微星', agentState: 'BUSY', status: 'running', currentTaskRuntimeStatus: 'running' }),
      ],
      requirementsWorkers: [],
      reviewWorkers: [],
      designWorkers: [],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [],
    },
    '⠦ 智能体启动中...',
    2,
  )
  expect(line).toContain('路由初始化 / 执行中')
})

test('derives busy footer text from overall review workers', () => {
  const line = resolveFooterProgressLine(
    {
      status: 'running',
      route: 'home',
      activeStage: 'stage.a08.start',
      activeStageLabel: '复核',
      routingWorkers: [],
      requirementsWorkers: [],
      reviewWorkers: [],
      designWorkers: [],
      taskSplitWorkers: [],
      developmentWorkers: [],
      overallReviewWorkers: [worker({ agentState: 'BUSY', status: 'running', currentTaskRuntimeStatus: 'running' })],
    },
    '',
    4,
  )
  expect(line).toContain('复核 / 审核中')
})
