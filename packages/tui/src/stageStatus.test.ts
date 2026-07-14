import { expect, test } from 'bun:test'
import {
  applyStageChanged,
  EMPTY_STAGE_CURSOR,
  inferBootstrapStatus,
  isNewRunnerGeneration,
  markTerminalStage,
  shouldAcceptProgressEvent,
} from './stageStatus'

test('authoritative runner failure stays terminal for runtime and worker-derived updates', () => {
  const running = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a06.start',
    status: 'running',
    source: 'runner_start',
    runner_id: 'runner-1',
    stage_seq: 7,
  })
  const failed = applyStageChanged(running.cursor, {
    action: 'stage.a06.start',
    status: 'failed',
    source: 'runner_failure',
    runner_id: 'runner-1',
    stage_seq: 7,
  })
  const staleRuntime = applyStageChanged(failed.cursor, {
    action: 'stage.a06.start',
    status: 'running',
    source: 'runtime_inference',
    stage_seq: 7,
  })

  expect(failed.accepted).toBe(true)
  expect(failed.authoritativeFailure).toBe(true)
  expect(staleRuntime.accepted).toBe(false)
  expect(shouldAcceptProgressEvent(failed.cursor, { action: 'stage.a06.start', stage_seq: 7 })).toBe(false)
})

test('only a newer runner generation can replace a failed generation', () => {
  const failed = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a05.start',
    status: 'failed',
    source: 'runner_failure',
    runner_id: 'runner-old',
    stage_seq: 11,
  })
  const sameRunner = applyStageChanged(failed.cursor, {
    action: 'stage.a05.start',
    status: 'running',
    source: 'runner_start',
    runner_id: 'runner-old',
    stage_seq: 12,
  })
  const restarted = applyStageChanged(failed.cursor, {
    action: 'stage.a05.start',
    status: 'running',
    source: 'runner_start',
    runner_id: 'runner-new',
    stage_seq: 12,
  })

  expect(sameRunner.accepted).toBe(false)
  expect(restarted.accepted).toBe(true)
  expect(restarted.cursor.terminalStatus).toBe('')
  expect(restarted.cursor.activeRunnerId).toBe('runner-new')
})

test('late events from an older runner cannot overwrite the new generation', () => {
  const oldFailure = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a07.start',
    status: 'failed',
    source: 'runner_failure',
    runner_id: 'runner-old',
    stage_seq: 3,
  })
  const restarted = applyStageChanged(oldFailure.cursor, {
    action: 'stage.a07.start',
    status: 'running',
    source: 'runner_start',
    runner_id: 'runner-new',
    stage_seq: 4,
  })
  const late = applyStageChanged(restarted.cursor, {
    action: 'stage.a07.start',
    status: 'failed',
    source: 'runner_failure',
    runner_id: 'runner-old',
    stage_seq: 3,
  })

  expect(late.accepted).toBe(false)
  expect(late.cursor.activeRunnerId).toBe('runner-new')
})

test('runner_complete can advance to the next substage under the same root runner', () => {
  const complete = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a03.start',
    status: 'completed',
    source: 'runner_complete',
    runner_id: 'root-runner',
    stage_seq: 5,
  })
  const next = applyStageChanged(complete.cursor, {
    action: 'stage.a04.start',
    status: 'running',
    source: 'runner_start',
    runner_id: 'root-runner',
    stage_seq: 6,
  })

  expect(next.accepted).toBe(true)
  expect(next.cursor.activeAction).toBe('stage.a04.start')
})

test('bootstrap uses snapshot.app only and never revives state from worker snapshots', () => {
  expect(inferBootstrapStatus({
    snapshots: {
      app: { active_stage: 'stage.a06.start', active_stage_status: 'failed' },
      stages: {
        'task-split': {
          workers: [{ session_name: '需求分析师', agent_state: 'BUSY', health_status: 'alive' }],
        },
      },
    },
  })).toBe('failed')

  expect(inferBootstrapStatus({
    snapshots: {
      app: { active_stage: 'stage.a06.start' },
      stages: {
        'task-split': {
          workers: [{ session_name: '需求分析师', agent_state: 'BUSY', health_status: 'alive' }],
        },
      },
    },
  })).toBe('ready')
})

test('bootstrap prioritizes persisted runner failure over HITL and running status', () => {
  expect(inferBootstrapStatus({
    snapshots: {
      app: {
        active_stage_status: 'running',
        pending_hitl: true,
        active_stage_failure: {
          status: 'failed',
          source: 'runner_failure',
          runner_id: 'runner-1',
        },
      },
    },
  })).toBe('failed')
})

test('HITL and recovery windows remain awaiting-input rather than terminal', () => {
  expect(inferBootstrapStatus({ snapshots: { app: { pending_hitl: true } } })).toBe('awaiting-input')
  expect(inferBootstrapStatus({ snapshots: { app: { active_stage_status: 'awaiting-input' } } })).toBe('awaiting-input')
})

test('generic error path marks only the current generation terminal', () => {
  const running = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a05.start',
    status: 'running',
    source: 'runner_start',
    runner_id: 'runner-1',
    stage_seq: 3,
  })
  const cursor = markTerminalStage(running.cursor)
  expect(cursor.terminalRunnerId).toBe('runner-1')
  expect(shouldAcceptProgressEvent(cursor, { action: 'stage.a05.start', runner_id: 'runner-1', stage_seq: 3 })).toBe(false)
})

test('explicit restart is confirmed only after a newer runner generation is observed', () => {
  const previous = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a06.start', status: 'failed', source: 'runner_failure', runner_id: 'old', stage_seq: 7,
  }).cursor
  const next = applyStageChanged(previous, {
    action: 'stage.a06.start', status: 'running', source: 'runner_start', runner_id: 'new', stage_seq: 8,
  }).cursor
  expect(isNewRunnerGeneration(previous, next)).toBe(true)
  expect(isNewRunnerGeneration(previous, { ...previous, activeStageSeq: 8 })).toBe(false)
})
