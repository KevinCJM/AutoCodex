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
      active_stage_message: 'M1-T1 评审第 1 轮',
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
      graphify: {
        mode: 'auto',
        state: 'ready',
        version: '0.9.27',
        freshness: 'fresh',
        node_count: 5098,
        edge_count: 22091,
        report_path: '/tmp/project/.tmux_workflow/evidence.md',
      },
      available_runs: [{ run_id: 'run_1', worker_count: 2, failed_count: 1 }],
    },
    stages: {
      development: {
        project_dir: '/tmp/project',
        requirement_name: '需求A',
        files: [{ label: '任务单', path: '/tmp/task.md', exists: true, updated_at: 'now', summary: 'ok' }],
        workers: [{
          worker_id: 'development-developer',
          state_path: '/tmp/worker.state.json',
          state_revision: 42,
          session_name: 'dev-1',
          result_status: 'failed',
          workflow_action: 'stage.a07.start',
          health_status: 'alive',
          dispatch_state: 'submitted',
          dispatch_reason: 'review',
          turn_state: 'orphaned',
          vendor: 'deveco',
          model: 'deveco/GLM-5.1',
          reasoning_effort: 'max',
          ponytail_mode: 'full',
          ponytail_bundle_version: '4.8.4',
          ponytail_delivery: 'runtime_prompt',
          requirements_mode: 'grill-with-docs',
          requirements_behavior: 'standard',
          grill_bundle_commit: 'ed37663c',
          grill_delivery: 'full',
          grill_question_seq: 3,
          graphify_freshness: 'fresh',
          graphify_usage_policy: 'query_required',
          graphify_evidence_delivery: 'confirmed',
          graphify_query_requirement: 'required',
          graphify_query_status: 'satisfied',
          graphify_query_command: 'affected',
          graphify_usage_receipt: 'usage.json',
          tmux_control_unavailable_since: '2026-07-14T10:00:00+08:00',
          artifact_paths: ['/tmp/out.md'],
        }],
        milestones: [{ key: 'M1', completed: false, tasks: [{ key: 'M1-T1', completed: true }] }],
        current_milestone_key: 'M1',
      },
    },
    control: { control_id: 'run_1', workers: [] },
    hitl: {
      pending: true,
      prompt_id: 'hitl-1',
      prompt_type: 'multiline',
      question_path: '/tmp/q.md',
      summary: 'question',
      attach_command: 'tmux attach -t dev-1',
      recovery_kind: 'agent_startup_intervention',
      reason_text: '需要登录',
      target_paths: ['/tmp/target.md'],
    },
    artifacts: { items: [{ path: '/tmp/out.md', updated_at: 'now', summary: 'out' }] },
    prompt: { pending: false },
  })

  expect(snapshots.app.projectDir).toBe('/tmp/project')
  expect(snapshots.app.availableRuns[0]?.runId).toBe('run_1')
  expect(snapshots.app.activeStageRunnerId).toBe('runner-9')
  expect(snapshots.app.activeStageMessage).toBe('M1-T1 评审第 1 轮')
  expect(snapshots.app.activeStageFailure?.message).toBe('failed safely')
  expect(snapshots.app.activeStageFailure?.orphanedWorkers[0]?.attachCommand).toBe('tmux attach -t dev-1')
  expect(snapshots.app.graphify).toMatchObject({
    mode: 'auto',
    state: 'ready',
    version: '0.9.27',
    nodeCount: 5098,
    edgeCount: 22091,
    reportPath: '/tmp/project/.tmux_workflow/evidence.md',
  })
  expect(snapshots.stages.development.workers[0]?.sessionName).toBe('dev-1')
  expect(snapshots.stages.development.workers[0]?.turnState).toBe('orphaned')
  expect(snapshots.stages.development.workers[0]).toMatchObject({
    workerId: 'development-developer',
    statePath: '/tmp/worker.state.json',
    stateRevision: 42,
    resultStatus: 'failed',
    workflowAction: 'stage.a07.start',
    dispatchState: 'submitted',
    dispatchReason: 'review',
    vendor: 'deveco',
    model: 'deveco/GLM-5.1',
    reasoningEffort: 'max',
    ponytailMode: 'full',
    ponytailBundleVersion: '4.8.4',
    ponytailDelivery: 'runtime_prompt',
    requirementsMode: 'grill-with-docs',
    requirementsBehavior: 'standard',
    grillBundleCommit: 'ed37663c',
    grillDelivery: 'full',
    grillQuestionSeq: 3,
    graphifyFreshness: 'fresh',
    graphifyUsagePolicy: 'query_required',
    graphifyEvidenceDelivery: 'confirmed',
    graphifyQueryRequirement: 'required',
    graphifyQueryStatus: 'satisfied',
    graphifyQueryCommand: 'affected',
    graphifyUsageReceipt: 'usage.json',
  })
  expect(snapshots.stages.development.workers[0]?.tmuxUnavailableSince).toBe('2026-07-14T10:00:00+08:00')
  expect(snapshots.stages.development.milestones[0]?.tasks[0]?.completed).toBe(true)
  expect(snapshots.hitl.questionPath).toBe('/tmp/q.md')
  expect(snapshots.hitl).toMatchObject({
    promptId: 'hitl-1',
    attachCommand: 'tmux attach -t dev-1',
    recoveryKind: 'agent_startup_intervention',
    reasonText: '需要登录',
    targetPaths: ['/tmp/target.md'],
  })
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

test('normalizePromptSnapshot preserves optional Grill metadata from outer snapshot and nested payload', () => {
  const prompt = normalizePromptSnapshot({
    pending: true,
    prompt_id: 'grill_1',
    prompt_type: 'select',
    interaction_kind: 'grill',
    question_index: 4,
    owner_runner_id: 'runner-a03',
    question_seq: 4,
    payload: {
      title: '如何定义业务边界？',
      recommendation: '保留现有边界',
      reason_text: '会影响后续设计',
    },
  })

  expect(prompt).toMatchObject({
    interactionKind: 'grill',
    questionIndex: 4,
    recommendation: '保留现有边界',
    reasonText: '会影响后续设计',
    ownerRunnerId: 'runner-a03',
    questionSeq: 4,
  })
})

test('normalizePromptSnapshot restores a submittable synthetic Grill multiline prompt', () => {
  const prompt = normalizePromptSnapshot({
    pending: true,
    prompt_id: 'grill_recovery_session-a_3_abc',
    prompt_type: 'multiline',
    owner_runner_id: 'grill-session:session-a',
    question_seq: 3,
    payload: {
      interaction_kind: 'grill',
      synthetic_recovery: true,
      can_submit: true,
      default_value: '方案 B',
      answer_options: ['方案 A', '方案 B'],
      grill_session_id: 'session-a',
      grill_question_hash: 'sha256:abc',
    },
  })

  expect(prompt).toMatchObject({
    pending: true,
    promptType: 'multiline',
    ownerRunnerId: 'grill-session:session-a',
    questionSeq: 3,
  })
  expect(prompt.payload).toMatchObject({
    can_submit: true,
    grill_session_id: 'session-a',
    grill_question_hash: 'sha256:abc',
  })
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
