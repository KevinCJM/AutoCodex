import { expect, test } from 'bun:test'
import {
  applyStageChanged,
  EMPTY_STAGE_CURSOR,
  inferBootstrapStatus,
  markTerminalStage,
  shouldAcceptProgressEvent,
} from './stageStatus'

test('failed stage keeps later progress from the same stage sequence from reviving running state', () => {
  const failed = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a05.start',
    status: 'failed',
    stage_seq: 7,
  })
  expect(failed.accepted).toBe(true)
  expect(failed.status).toBe('failed')
  expect(
    shouldAcceptProgressEvent(failed.cursor, {
      action: 'stage.a05.start',
      stage_seq: 7,
    }),
  ).toBe(false)
})

test('newer stage sequence clears terminal lock and accepts fresh progress', () => {
  const failed = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a05.start',
    status: 'failed',
    stage_seq: 7,
  })
  const restarted = applyStageChanged(failed.cursor, {
    action: 'stage.a05.start',
    status: 'running',
    stage_seq: 8,
  })
  expect(restarted.accepted).toBe(true)
  expect(restarted.status).toBe('running')
  expect(
    shouldAcceptProgressEvent(restarted.cursor, {
      action: 'stage.a05.start',
      stage_seq: 7,
    }),
  ).toBe(false)
  expect(
    shouldAcceptProgressEvent(restarted.cursor, {
      action: 'stage.a05.start',
      stage_seq: 8,
    }),
  ).toBe(true)
})

test('newer stage sequence also revives awaiting-input after failure', () => {
  const failed = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a07.start',
    status: 'failed',
    stage_seq: 11,
  })
  const awaiting = applyStageChanged(failed.cursor, {
    action: 'stage.a07.start',
    status: 'awaiting-input',
    stage_seq: 12,
  })
  expect(awaiting.accepted).toBe(true)
  expect(awaiting.status).toBe('awaiting-input')
  expect(
    shouldAcceptProgressEvent(awaiting.cursor, {
      action: 'stage.a07.start',
      stage_seq: 12,
    }),
  ).toBe(true)
})

test('error path can mark the current stage as terminal before failed stage.changed arrives', () => {
  const running = applyStageChanged(EMPTY_STAGE_CURSOR, {
    action: 'stage.a05.start',
    status: 'running',
    stage_seq: 3,
  })
  const cursor = markTerminalStage(running.cursor)
  expect(
    shouldAcceptProgressEvent(cursor, {
      action: 'stage.a05.start',
      stage_seq: 3,
    }),
  ).toBe(false)
})

test('bootstrap infers running when active stage has a busy worker', () => {
  expect(
    inferBootstrapStatus({
      snapshots: {
        app: {
          active_stage: 'stage.a07.start',
          pending_hitl: false,
        },
        stages: {
          development: {
            workers: [
              {
                session_name: '开发工程师-天猛星',
                status: 'running',
                agent_state: 'BUSY',
                health_status: 'alive',
                current_task_runtime_status: 'running',
                session_exists: true,
              },
            ],
          },
        },
      },
    }),
  ).toBe('running')
})

test('bootstrap maps control session action to routing stage workers', () => {
  expect(
    inferBootstrapStatus({
      snapshots: {
        app: {
          active_stage: 'control.b01.open',
          pending_hitl: false,
        },
        stages: {
          routing: {
            workers: [
              {
                session_name: '路由初始化-角木蛟',
                status: 'running',
                agent_state: 'BUSY',
                health_status: 'alive',
                current_task_runtime_status: 'running',
                session_exists: true,
              },
            ],
          },
        },
      },
    }),
  ).toBe('running')
})

test('bootstrap keeps awaiting-input ahead of worker activity', () => {
  expect(
    inferBootstrapStatus({
      snapshots: {
        app: {
          active_stage: 'stage.a07.start',
          pending_hitl: true,
        },
        stages: {
          development: {
            workers: [
              {
                session_name: '开发工程师-天猛星',
                status: 'running',
                agent_state: 'BUSY',
                health_status: 'alive',
                current_task_runtime_status: 'running',
                session_exists: true,
              },
            ],
          },
        },
      },
    }),
  ).toBe('awaiting-input')
})
