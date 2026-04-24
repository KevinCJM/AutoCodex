import { expect, test } from 'bun:test'
import { buildHomeAgents, isRunningWorker, resolveHomeAgentState } from './homeAgents'
import type { WorkerSnapshot } from './types'

function worker(overrides: Partial<WorkerSnapshot> = {}): WorkerSnapshot {
  return {
    workDir: '/tmp/project',
    sessionName: 'sess-1',
    status: 'running',
    workflowStage: 'create_running',
    agentState: 'waiting_input',
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

test('buildHomeAgents prefers DEAD when timestamps collide for the same session', () => {
  const agents = buildHomeAgents([
    {
      source: 'control',
      workers: [worker({ sessionName: 'sess-1', sessionExists: true, agentState: 'READY', updatedAt: '2026-04-22T10:00:00+08:00' })],
    },
    {
      source: 'development',
      workers: [worker({ sessionName: 'sess-1', sessionExists: false, agentState: 'DEAD', updatedAt: '2026-04-22T10:00:00+08:00' })],
    },
  ])
  expect(agents).toHaveLength(1)
  expect(agents[0]?.agentState).toBe('DEAD')
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
  expect(resolveHomeAgentState(worker({ agentState: '', status: 'running' }))).toBe('BUSY')
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
