import { expect, test } from 'bun:test'
import { buildHomeAgents, isRunningWorker, resolveHomeAgentState } from './homeAgents'
import type { WorkerSnapshot } from './types'

function worker(overrides: Partial<WorkerSnapshot> = {}): WorkerSnapshot {
  return {
    workDir: '/tmp/project',
    sessionName: 'sess-1',
    status: 'running',
    workflowStage: 'create_running',
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

test('isRunningWorker prioritizes sessionExists over healthStatus', () => {
  expect(isRunningWorker(worker({ sessionExists: true, healthStatus: 'unknown' }))).toBe(true)
  expect(isRunningWorker(worker({ sessionExists: false, healthStatus: 'alive' }))).toBe(false)
})

test('isRunningWorker keeps workers already marked DEAD so home can show failed agents', () => {
  expect(isRunningWorker(worker({ sessionExists: true, agentState: 'DEAD' }))).toBe(true)
})

test('isRunningWorker keeps prelaunch STARTING workers before tmux session exists', () => {
  expect(isRunningWorker(worker({ sessionExists: false, agentState: 'STARTING', healthStatus: 'unknown' }))).toBe(true)
})

test('buildHomeAgents prefers newer DEAD snapshot over older READY snapshot for the same session', () => {
  const agents = buildHomeAgents([
    {
      source: 'control',
      workers: [worker({ sessionName: 'sess-1', sessionExists: true, agentState: 'READY', updatedAt: '2026-04-22T10:00:00+08:00' })],
    },
    {
      source: 'development',
      workers: [worker({ sessionName: 'sess-1', sessionExists: false, agentState: 'DEAD', updatedAt: '2026-04-22T10:00:01+08:00' })],
    },
  ])
  expect(agents).toHaveLength(1)
  expect(agents[0]).toMatchObject({
    source: 'development',
    sessionName: 'sess-1',
    agentState: 'DEAD',
  })
})

test('buildHomeAgents prefers live state over DEAD when timestamps collide for the same session', () => {
  const agents = buildHomeAgents([
    {
      source: 'control',
      workers: [worker({ sessionName: 'sess-1', sessionExists: true, agentState: 'DEAD', updatedAt: '2026-04-22T10:00:00+08:00' })],
    },
    {
      source: 'development',
      workers: [worker({ sessionName: 'sess-1', sessionExists: true, agentState: 'READY', updatedAt: '2026-04-22T10:00:00+08:00' })],
    },
  ])
  expect(agents).toHaveLength(1)
  expect(agents[0]?.agentState).toBe('READY')
})

test('buildHomeAgents limits home overview to control and current stage sources when active stage is known', () => {
  const agents = buildHomeAgents(
    [
      {
        source: 'requirements',
        workers: [worker({ sessionName: '分析师-天慧星', sessionExists: false, agentState: 'DEAD' })],
      },
      {
        source: 'development',
        workers: [worker({ sessionName: '开发工程师-天猛星', sessionExists: true, agentState: 'BUSY' })],
      },
      {
        source: 'control',
        workers: [worker({ sessionName: '控制台-当前运行', sessionExists: true, agentState: 'READY' })],
      },
    ],
    'stage.a07.start',
  )
  expect(agents).toHaveLength(2)
  expect(agents.map((agent) => agent.sessionName)).toEqual(['开发工程师-天猛星', '控制台-当前运行'])
})

test('buildHomeAgents keeps current-stage DEAD worker visible for requirements stage', () => {
  const agents = buildHomeAgents(
    [
      {
        source: 'requirements',
        workers: [worker({ sessionName: '分析师-天慧星', sessionExists: false, agentState: 'DEAD' })],
      },
      {
        source: 'development',
        workers: [worker({ sessionName: '开发工程师-天猛星', sessionExists: true, agentState: 'BUSY' })],
      },
    ],
    'stage.a03.start',
  )
  expect(agents).toHaveLength(1)
  expect(agents[0]).toMatchObject({
    source: 'requirements',
    sessionName: '分析师-天慧星',
    agentState: 'DEAD',
  })
})

test('buildHomeAgents keeps cross-stage aggregation when active stage is unknown', () => {
  const agents = buildHomeAgents([
    {
      source: 'requirements',
      workers: [worker({ sessionName: '分析师-天慧星', sessionExists: false, agentState: 'DEAD' })],
    },
    {
      source: 'development',
      workers: [worker({ sessionName: '开发工程师-天猛星', sessionExists: true, agentState: 'BUSY' })],
    },
  ])
  expect(agents).toHaveLength(2)
  expect(agents.map((agent) => agent.sessionName)).toEqual(['分析师-天慧星', '开发工程师-天猛星'])
})

test('buildHomeAgents prefers fresher live heartbeat over stale dead snapshot for the same session', () => {
  const agents = buildHomeAgents([
    {
      source: 'control',
      workers: [worker({
        sessionName: 'sess-1',
        sessionExists: false,
        agentState: 'DEAD',
        updatedAt: '2026-04-22T10:00:01+08:00',
      })],
    },
    {
      source: 'design',
      workers: [worker({
        sessionName: 'sess-1',
        sessionExists: true,
        agentState: 'BUSY',
        updatedAt: '2026-04-22T10:00:00+08:00',
        lastHeartbeatAt: '2026-04-22T10:00:02+08:00',
      })],
    },
  ])
  expect(agents).toHaveLength(1)
  expect(agents[0]).toMatchObject({
    source: 'design',
    sessionName: 'sess-1',
    agentState: 'BUSY',
  })
})

test('buildHomeAgents keeps control workers when duplicate sessions exist across sources', () => {
  const agents = buildHomeAgents([
    { source: 'control', workers: [worker({ sessionName: 'sess-1', sessionExists: true })] },
    { source: 'routing', workers: [worker({ sessionName: 'sess-1', sessionExists: true, workDir: '/tmp/other' })] },
    { source: 'requirements', workers: [worker({ sessionName: 'sess-2', sessionExists: true })] },
  ])
  expect(agents).toHaveLength(2)
  expect(agents[0]).toMatchObject({
    source: 'control',
    sessionName: 'sess-1',
    attachCommand: 'tmux attach -t sess-1',
  })
  expect(agents[1]).toMatchObject({
    source: 'requirements',
    sessionName: 'sess-2',
  })
})

test('resolveHomeAgentState preserves READY when backend already reports ready', () => {
  expect(resolveHomeAgentState(worker({ agentState: 'READY', status: 'running' }))).toBe('READY')
  expect(resolveHomeAgentState(worker({ agentState: 'READY', status: 'ready', currentTaskRuntimeStatus: 'running' }))).toBe('READY')
  expect(resolveHomeAgentState(worker({ agentState: 'STARTING', status: 'running' }))).toBe('STARTING')
  expect(resolveHomeAgentState(worker({ agentState: '', status: 'running' }))).toBe('UNKNOWN')
})

test('buildHomeAgents keeps newer READY development state over stale control DEAD', () => {
  const agents = buildHomeAgents(
    [
      {
        source: 'control',
        workers: [
          worker({
            sessionName: '开发工程师-天速星',
            sessionExists: true,
            agentState: 'DEAD',
            healthStatus: 'alive',
            updatedAt: '2026-04-22T10:00:00+08:00',
          }),
        ],
      },
      {
        source: 'development',
        workers: [
          worker({
            sessionName: '开发工程师-天速星',
            sessionExists: true,
            agentState: 'READY',
            healthStatus: 'alive',
            updatedAt: '2026-04-22T10:00:01+08:00',
          }),
        ],
      },
    ],
    'stage.a07.start',
  )
  expect(agents).toHaveLength(1)
  expect(agents[0]).toMatchObject({
    source: 'development',
    sessionName: '开发工程师-天速星',
    agentState: 'READY',
  })
})

test('buildHomeAgents keeps backend READY state for running development worker', () => {
  const agents = buildHomeAgents([
    {
      source: 'development',
      workers: [
        worker({
          sessionName: '开发工程师-亢金龙',
          sessionExists: true,
          agentState: 'READY',
          status: 'running',
          currentTaskRuntimeStatus: 'running',
        }),
      ],
    },
  ])
  expect(agents).toHaveLength(1)
  expect(agents[0]).toMatchObject({
    source: 'development',
    sessionName: '开发工程师-亢金龙',
    agentState: 'READY',
  })
})

test('buildHomeAgents shows prelaunch routing workers as STARTING', () => {
  const agents = buildHomeAgents(
    [
      {
        source: 'routing',
        workers: [
          worker({
            sessionName: '路由器-天伤星',
            sessionExists: false,
            agentState: 'STARTING',
            healthStatus: 'unknown',
            status: 'pending',
            workflowStage: 'pending',
          }),
        ],
      },
    ],
    'stage.a01.start',
  )
  expect(agents).toHaveLength(1)
  expect(agents[0]).toMatchObject({
    source: 'routing',
    sessionName: '路由器-天伤星',
    healthStatus: 'unknown',
    agentState: 'STARTING',
  })
})
