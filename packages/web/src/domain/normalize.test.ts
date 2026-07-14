import { expect, test } from 'bun:test'
import { normalizeBootstrapPayload, normalizePromptSnapshot, normalizeRequirementsList, normalizeSnapshotsPayload } from './normalize'

test('normalizeSnapshotsPayload maps bridge snapshots into web state', () => {
  const snapshots = normalizeSnapshotsPayload({
    app: {
      project_dir: '/tmp/project',
      requirement_name: '需求A',
      active_stage: 'stage.a07.start',
      active_stage_status: 'failed',
      active_stage_seq: 9,
      active_stage_runner_id: 'runner-9',
      active_stage_source: 'runner_failure',
      active_stage_label: '任务开发',
      active_stage_failure: {
        action: 'stage.a07.start',
        status: 'failed',
        source: 'runner_failure',
        runner_id: 'runner-9',
        stage_seq: 9,
        message: 'failed safely',
        failure_path: '/tmp/failure.json',
        orphaned_workers: [{ session_name: 'dev-1' }],
      },
      pending_hitl: true,
      available_runs: [{ run_id: 'run_1', worker_count: 2, failed_count: 1 }],
    },
    stages: {
      development: {
        project_dir: '/tmp/project',
        requirement_name: '需求A',
        files: [{ label: '任务单', path: '/tmp/task.md', exists: true, updated_at: 'now', summary: 'ok' }],
        workers: [{
          session_name: 'dev-1',
          health_status: 'alive',
          turn_state: 'orphaned',
          tmux_control_unavailable_since: '2026-07-14T10:00:00+08:00',
          artifact_paths: ['/tmp/out.md'],
        }],
        milestones: [{ key: 'M1', completed: false, tasks: [{ key: 'M1-T1', completed: true }] }],
        current_milestone_key: 'M1',
      },
    },
    control: { control_id: 'run_1', workers: [] },
    hitl: { pending: true, question_path: '/tmp/q.md', summary: 'question' },
    artifacts: { items: [{ path: '/tmp/out.md', updated_at: 'now', summary: 'out' }] },
    prompt: { pending: false },
  })

  expect(snapshots.app.projectDir).toBe('/tmp/project')
  expect(snapshots.app.availableRuns[0]?.runId).toBe('run_1')
  expect(snapshots.app.activeStageRunnerId).toBe('runner-9')
  expect(snapshots.app.activeStageFailure?.message).toBe('failed safely')
  expect(snapshots.app.activeStageFailure?.orphanedWorkers[0]?.attachCommand).toBe('tmux attach -t dev-1')
  expect(snapshots.stages.development.workers[0]?.sessionName).toBe('dev-1')
  expect(snapshots.stages.development.workers[0]?.turnState).toBe('orphaned')
  expect(snapshots.stages.development.workers[0]?.tmuxUnavailableSince).toBe('2026-07-14T10:00:00+08:00')
  expect(snapshots.stages.development.milestones[0]?.tasks[0]?.completed).toBe(true)
  expect(snapshots.hitl.questionPath).toBe('/tmp/q.md')
})

test('normalizePromptSnapshot preserves prompt payload for refresh recovery', () => {
  const prompt = normalizePromptSnapshot({
    pending: true,
    prompt_id: 'prompt_1',
    prompt_type: 'select',
    payload: { default_value: 'gemini', options: [{ value: 'gemini', label: 'Gemini' }] },
  })

  expect(prompt.pending).toBe(true)
  expect(prompt.promptId).toBe('prompt_1')
  expect(prompt.payload.default_value).toBe('gemini')
})

test('normalize app merges persistent failure details with authoritative app generation', () => {
  const snapshots = normalizeSnapshotsPayload({
    app: {
      active_stage: 'stage.a06.start',
      active_stage_status: 'failed',
      active_stage_seq: 7,
      active_stage_runner_id: 'runner-7',
      active_stage_source: 'runner_failure',
      active_stage_label: '任务拆分',
      active_stage_failure: {
        kind: 'runner_failure',
        message: 'persisted failure',
        path: '/tmp/persisted.failure.json',
      },
    },
  })
  expect(snapshots.app.activeStageFailure).toMatchObject({
    action: 'stage.a06.start',
    status: 'failed',
    source: 'runner_failure',
    runnerId: 'runner-7',
    stageSeq: 7,
    failureKind: 'runner_failure',
    failurePath: '/tmp/persisted.failure.json',
  })
})

test('normalizeBootstrapPayload handles missing optional sections', () => {
  const bootstrap = normalizeBootstrapPayload({
    protocol_version: '1.0',
    python_path: '/usr/bin/python',
    routes: ['home'],
    commands: ['app.bootstrap'],
    snapshots: {},
  })

  expect(bootstrap.protocolVersion).toBe('1.0')
  expect(bootstrap.snapshots.stages.routing.files).toEqual([])
})

test('normalizeRequirementsList maps existing requirement options', () => {
  const result = normalizeRequirementsList({
    schema_version: '1.0',
    project_dir: '/tmp/project',
    requirements: [{ name: '需求A', path: '/tmp/project/需求A_原始需求.md' }, { name: '', path: '/tmp/empty.md' }],
  })

  expect(result.schemaVersion).toBe('1.0')
  expect(result.projectDir).toBe('/tmp/project')
  expect(result.requirements).toEqual([{ name: '需求A', path: '/tmp/project/需求A_原始需求.md' }])
})
