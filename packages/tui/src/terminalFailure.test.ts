import { expect, test } from 'bun:test'
import {
  formatStageFailureReport,
  isAuthoritativeStageFailure,
  normalizeAppStageFailure,
  normalizeStageFailure,
  stageFailureMatchesGeneration,
} from './terminalFailure'

test('normalizes runner failure and every orphan attach command', () => {
  const failure = normalizeStageFailure({
    action: 'stage.a06.start',
    stage_label: '任务拆分',
    status: 'failed',
    source: 'runner_failure',
    runner_id: 'runner-1',
    stage_seq: 8,
    message: 'tmux control unavailable',
    failure_path: '/tmp/stage.failure.json',
    orphaned_workers: [
      { session_name: '需求分析师-天哭星' },
      { session_name: '审核员 1', attach_command: ['tmux', 'attach', '-t', '审核员 1'] },
    ],
  })

  expect(isAuthoritativeStageFailure(failure)).toBe(true)
  expect(failure?.orphanedWorkers.map((worker) => worker.attachCommand)).toEqual([
    'tmux attach -t 需求分析师-天哭星',
    "tmux attach -t '审核员 1'",
  ])
  const report = formatStageFailureReport(failure!)
  expect(report).toContain('阶段: 任务拆分')
  expect(report).toContain('原因: tmux control unavailable')
  expect(report).toContain('失败记录: /tmp/stage.failure.json')
  expect(report).not.toContain('保留的智能体现场')
  expect(report).not.toContain('tmux attach')
})

test('HITL and recoverable errors are not authoritative terminal failures', () => {
  expect(isAuthoritativeStageFailure(normalizeStageFailure({
    status: 'awaiting-input',
    source: 'runner_start',
    message: 'login required',
  }))).toBe(false)
  expect(isAuthoritativeStageFailure(normalizeStageFailure({
    status: 'failed',
    source: 'runtime_inference',
  }))).toBe(false)
})

test('persistent app failure merges legacy kind/path payload with app generation fields', () => {
  const failure = normalizeAppStageFailure({
    active_stage: 'stage.a06.start',
    active_stage_label: '任务拆分',
    active_stage_status: 'failed',
    active_stage_source: 'runner_failure',
    active_stage_runner_id: 'runner-7',
    active_stage_seq: 7,
    active_stage_failure: {
      kind: 'runner_failure',
      message: 'persisted failure',
      path: '/tmp/persisted.failure.json',
      runner_id: 'runner-7',
      stage_seq: 7,
    },
  })
  expect(isAuthoritativeStageFailure(failure)).toBe(true)
  expect(failure?.failureKind).toBe('runner_failure')
  expect(failure?.failurePath).toBe('/tmp/persisted.failure.json')
  expect(failure?.action).toBe('stage.a06.start')
})

test('an explicit restart only releases a failure from its active generation', () => {
  const oldFailure = normalizeStageFailure({
    status: 'failed', source: 'runner_failure', runner_id: 'runner-old', stage_seq: 7,
  })
  const newFailure = normalizeStageFailure({
    status: 'failed', source: 'runner_failure', runner_id: 'runner-new', stage_seq: 8,
  })
  const activeGeneration = { activeRunnerId: 'runner-new', activeStageSeq: 8 }

  expect(stageFailureMatchesGeneration(oldFailure, activeGeneration)).toBe(false)
  expect(stageFailureMatchesGeneration(newFailure, activeGeneration)).toBe(true)
})
