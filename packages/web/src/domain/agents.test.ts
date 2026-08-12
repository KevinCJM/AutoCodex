import { expect, test } from 'bun:test'
import { buildAgentConfigLabel, buildHomeAgents, isBusyTurnWorker, reconcileWorkerSnapshots, resolveAgentProgressLine, resolveAgentState, workerOwnedPromptIsReady } from './agents'
import type { PromptSnapshot, WorkerSnapshot } from './types'

function worker(overrides: Partial<WorkerSnapshot> = {}): WorkerSnapshot {
  return {
    workerId: 'worker-1', statePath: '/tmp/worker.state.json', workDir: '/tmp/project', sessionName: '开发工程师-天罡星',
    status: 'running', resultStatus: '', workflowAction: 'stage.a07.start', workflowStage: 'development', agentState: 'BUSY',
    healthStatus: 'alive', currentTaskRuntimeStatus: 'running', dispatchState: 'submitted', dispatchReason: '', turnState: 'waiting_result',
    tmuxControlStatus: 'available', tmuxControlError: '', tmuxUnavailableSince: '', stageRunnerId: 'runner-7', orphanedAt: '', orphanedReason: '',
    vendor: 'deveco', model: 'deveco/GLM-5.1', resolvedModel: '', reasoningEffort: 'max', retryCount: 0, note: '',
    transcriptPath: '', turnStatusPath: '/tmp/turn.json', questionPath: '', answerPath: '', artifactPaths: [], sessionExists: true,
    lastHeartbeatAt: '2026-07-15T12:00:00+08:00', updatedAt: '2026-07-15T12:00:00+08:00',
    ...overrides,
  }
}

test('explicit terminal state wins over turn lifecycle fields', () => {
  expect(resolveAgentState(worker({ agentState: 'BUSY', status: 'succeeded', turnState: 'succeeded' }))).toBe('BUSY')
  expect(resolveAgentState(worker({ agentState: 'READY', status: 'running', turnState: 'waiting_result' }))).toBe('READY')
})

test('worker-owned prompt requires matching READY owner revision', () => {
  const prompt: PromptSnapshot = {
    pending: true,
    promptId: 'prompt-ready-gate',
    promptType: 'multiline',
    payload: {
      ready_for_human: true,
      owner_session_name: '开发工程师-天罡星',
      owner_turn_id: 'turn-1',
      owner_state_revision: 7,
    },
  }
  expect(workerOwnedPromptIsReady(prompt, [worker({
    stateRevision: 6,
    agentState: 'BUSY',
    turnState: 'succeeded',
    currentTaskRuntimeStatus: 'done',
    currentTurnId: 'turn-1',
  })])).toBe(false)
  expect(workerOwnedPromptIsReady(prompt, [worker({
    stateRevision: 7,
    agentState: 'READY',
    turnState: 'succeeded',
    currentTaskRuntimeStatus: 'done',
    currentTurnId: 'turn-1',
  })])).toBe(true)
})

test('home agents reject old runner and prefer the newest state revision', () => {
  const stale = worker({ stateRevision: 41, agentState: 'BUSY' })
  const current = worker({ stateRevision: 42, agentState: 'READY', status: 'succeeded', turnState: 'succeeded' })
  const oldRunner = worker({ sessionName: '审核员-旧', stageRunnerId: 'runner-old', stateRevision: 99 })
  const agents = buildHomeAgents([
    { source: 'development', workers: [stale, oldRunner] },
    { source: 'control', workers: [current] },
  ], 'stage.a07.start', 'runner-7')

  expect(agents).toHaveLength(1)
  expect(agents[0]).toMatchObject({ agentState: 'READY', agentConfigLabel: 'DevEco Code | deveco/GLM-5.1, Max' })
})

test('agent config label appends Ponytail mode and keeps legacy labels unchanged', () => {
  expect(buildAgentConfigLabel(worker({ ponytailMode: 'full' }))).toBe('DevEco Code | deveco/GLM-5.1, Max | Ponytail Full')
  expect(buildAgentConfigLabel(worker({ ponytailMode: 'off' }))).toBe('DevEco Code | deveco/GLM-5.1, Max | Ponytail Off')
  expect(buildAgentConfigLabel(worker())).toBe('DevEco Code | deveco/GLM-5.1, Max')
})

test('agent config label appends Grill modes and hides Standard', () => {
  expect(buildAgentConfigLabel(worker({ requirementsMode: 'grill' }))).toBe('DevEco Code | deveco/GLM-5.1, Max | Grill Me')
  expect(buildAgentConfigLabel(worker({ requirementsMode: 'grill-with-docs' }))).toBe('DevEco Code | deveco/GLM-5.1, Max | Grill with Docs')
  expect(buildAgentConfigLabel(worker({
    requirementsMode: 'grill-with-docs',
    requirementsBehavior: 'standard',
  }))).toBe('DevEco Code | deveco/GLM-5.1, Max | Grill with Docs · 已完成')
  expect(buildAgentConfigLabel(worker({ requirementsMode: 'standard' }))).toBe('DevEco Code | deveco/GLM-5.1, Max')
  expect(buildAgentConfigLabel(worker({ ponytailMode: 'full', requirementsMode: 'grill' }))).toBe(
    'DevEco Code | deveco/GLM-5.1, Max | Ponytail Full | Grill Me',
  )
})

test('completed turn does not count as active work even if terminal tail is BUSY', () => {
  expect(isBusyTurnWorker(worker({ agentState: 'BUSY', turnState: 'succeeded', currentTaskRuntimeStatus: 'succeeded' }))).toBe(false)
})

test('snapshot reconciliation refuses a lower revision from a slow response', () => {
  const current = worker({ stateRevision: 12, agentState: 'READY' })
  const slow = worker({ stateRevision: 11, agentState: 'BUSY' })
  expect(reconcileWorkerSnapshots([current], [slow])[0]?.agentState).toBe('READY')
})

test('current-stage agents use the same fixed reviewer order as TUI', () => {
  const architect = worker({ workerId: 'development-review-架构师', sessionName: '架构师-地强星' })
  const analyst = worker({ workerId: 'development-review-需求分析师', sessionName: '需求分析师-天寿星' })
  const tester = worker({ workerId: 'development-review-测试工程师', sessionName: '测试工程师-地理星' })
  const agents = buildHomeAgents([{ source: 'development', workers: [architect, tester, analyst] }], 'stage.a07.start', 'runner-7')
  expect(agents.map((agent) => agent.workerId)).toEqual([
    'development-review-需求分析师',
    'development-review-测试工程师',
    'development-review-架构师',
  ])
})

test('live turn replaces stale startup message like the TUI footer', () => {
  expect(resolveAgentProgressLine({
    status: 'running',
    action: 'stage.a05.start',
    activeRunnerId: 'runner-5',
    stageLabel: '详细设计',
    stageMessage: '等待 tmux',
    explicitProgress: '',
    workers: [worker({ workflowAction: 'stage.a05.start', stageRunnerId: 'runner-5' })],
  })).toBe('详细设计 / 审核中')
})
