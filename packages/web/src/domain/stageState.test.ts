import { expect, test } from 'bun:test'
import {
  applyStageGeneration,
  bindProgressEntry,
  EMPTY_STAGE_GENERATION,
  progressEntryMatchesCursor,
  resolveStageMessage,
  shouldAcceptProgressEvent,
  shouldResetProgressForStageChange,
  stageFailureFromEvent,
} from './stageState'

test('web keeps authoritative runner failure ahead of runtime inference', () => {
  const failed = applyStageGeneration(EMPTY_STAGE_GENERATION, {
    action: 'stage.a06.start',
    status: 'failed',
    source: 'runner_failure',
    runner_id: 'runner-1',
    stage_seq: 9,
  })
  const stale = applyStageGeneration(failed.cursor, {
    action: 'stage.a06.start',
    status: 'running',
    source: 'runtime_inference',
    stage_seq: 9,
  })
  expect(failed.authoritativeFailure).toBe(true)
  expect(stale.accepted).toBe(false)
})

test('web accepts explicit new runner and rejects late old runner', () => {
  const failed = applyStageGeneration(EMPTY_STAGE_GENERATION, {
    action: 'stage.a07.start', status: 'failed', source: 'runner_failure', runner_id: 'old', stage_seq: 2,
  })
  const restarted = applyStageGeneration(failed.cursor, {
    action: 'stage.a07.start', status: 'running', source: 'runner_start', runner_id: 'new', stage_seq: 3,
  })
  const late = applyStageGeneration(restarted.cursor, {
    action: 'stage.a07.start', status: 'failed', source: 'runner_failure', runner_id: 'old', stage_seq: 2,
  })
  expect(restarted.accepted).toBe(true)
  expect(late.accepted).toBe(false)
})

test('web builds persistent failure details from stage event', () => {
  const failure = stageFailureFromEvent({
    action: 'stage.a06.start',
    status: 'failed',
    source: 'runner_failure',
    runner_id: 'runner-1',
    stage_seq: 9,
    message: 'tmux unavailable',
    failure_path: '/tmp/failure.json',
    orphaned_workers: [{ session_name: '分析师-1' }],
  })
  expect(failure?.failurePath).toBe('/tmp/failure.json')
  expect(failure?.orphanedWorkers[0]?.attachCommand).toBe('tmux attach -t 分析师-1')
})

test('progress is scoped to the active runner generation', () => {
  const design = applyStageGeneration(EMPTY_STAGE_GENERATION, {
    action: 'stage.a05.start', status: 'running', source: 'runner_start', runner_id: 'design-1', stage_seq: 3,
  })
  const entry = bindProgressEntry(design.cursor, {
    action: 'stage.a05.start', runner_id: 'design-1', stage_seq: 3,
  }, '详细设计 / 评审中')
  expect(entry).not.toBeNull()
  expect(progressEntryMatchesCursor(design.cursor, entry!)).toBe(true)
  expect(shouldAcceptProgressEvent(design.cursor, {
    action: 'stage.a05.start', runner_id: 'old', stage_seq: 3,
  })).toBe(false)

  const taskSplit = applyStageGeneration(design.cursor, {
    action: 'stage.a06.start', status: 'running', source: 'runner_start', runner_id: 'split-1', stage_seq: 4,
  })
  expect(shouldResetProgressForStageChange(design.cursor, taskSplit.cursor, taskSplit.status)).toBe(true)
  expect(progressEntryMatchesCursor(taskSplit.cursor, entry!)).toBe(false)
  expect(resolveStageMessage(design.cursor, taskSplit.cursor, '旧进度', '')).toBe('')
})

test('unknown runner-prefixed source cannot bypass authoritative runner scope', () => {
  const running = applyStageGeneration(EMPTY_STAGE_GENERATION, {
    action: 'stage.a05.start', status: 'running', source: 'runner_start', runner_id: 'runner-1', stage_seq: 5,
  })
  const spoofed = applyStageGeneration(running.cursor, {
    action: 'stage.a05.start', status: 'ready', source: 'runner_guess', stage_seq: 5,
  })
  expect(spoofed.accepted).toBe(false)
})
