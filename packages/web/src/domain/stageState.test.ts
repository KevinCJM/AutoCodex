import { expect, test } from 'bun:test'
import { applyStageGeneration, EMPTY_STAGE_GENERATION, stageFailureFromEvent } from './stageState'

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
