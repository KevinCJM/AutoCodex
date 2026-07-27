import type {
  AgentCatalog,
  AgentModelOption,
  AgentVendorOption,
  AppSnapshot,
  ArtifactItem,
  ArtifactsSnapshot,
  BootstrapPayload,
  ControlSnapshot,
  FilePreview,
  FileSnapshot,
  HitlSnapshot,
  PromptSnapshot,
  RequirementOption,
  RequirementsList,
  RunOption,
  SnapshotsPayload,
  StageRoute,
  StageFailureSnapshot,
  StageFailureWorker,
  StageSnapshot,
  WorkerSnapshot,
} from './types'
import { STAGE_ROUTES } from './stages'
import { normalizeGraphifyStatus } from './graphifyStatus'
import { resolvePromptMetadata } from './promptMetadata'

export const EMPTY_STAGE: StageSnapshot = {
  projectDir: '',
  requirementName: '',
  files: [],
  workers: [],
  blockers: [],
  statusText: '',
  done: false,
  milestones: [],
  currentMilestoneKey: '',
  allTasksCompleted: false,
}

export const EMPTY_CONTROL: ControlSnapshot = {
  supported: true,
  controlId: '',
  runId: '',
  runtimeDir: '',
  statusText: '',
  helpText: '',
  workers: [],
  done: false,
  canSwitchRuns: true,
  finalSummary: '',
  transitionText: '',
}

export const EMPTY_APP: AppSnapshot = {
  projectDir: '',
  requirementName: '',
  currentAction: '',
  activeRunId: '',
  activeStage: 'idle',
  activeStageStatus: 'ready',
  activeStageSeq: 0,
  activeStageRunnerId: '',
  activeStageSource: '',
  activeStageLabel: '等待中',
  activeStageMessage: '',
  activeStageFailure: null,
  pendingHitl: false,
  pendingAttention: false,
  pendingAttentionReason: '',
  pendingAttentionSince: '',
  recentArtifacts: [],
  availableRuns: [],
  capabilities: {},
}

export const EMPTY_HITL: HitlSnapshot = {
  pending: false,
  promptId: '',
  promptType: '',
  questionPath: '',
  answerPath: '',
  summary: '',
  attachCommand: '',
  recoveryKind: '',
  reasonText: '',
  targetPaths: [],
}

export const EMPTY_PROMPT: PromptSnapshot = {
  pending: false,
  promptId: '',
  promptType: '',
  payload: {},
}

function objectOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function str(value: unknown): string {
  return String(value ?? '')
}

function bool(value: unknown): boolean {
  return Boolean(value)
}

function num(value: unknown): number {
  const next = Number(value)
  return Number.isFinite(next) ? next : 0
}

function artifact(value: unknown): ArtifactItem {
  const item = objectOf(value)
  return {
    path: str(item.path),
    updatedAt: str(item.updated_at ?? item.updatedAt),
    summary: str(item.summary),
  }
}

function shellQuote(value: string): string {
  if (value && !/[\s'"`$;&|<>(){}\[\]*?!\\]/u.test(value)) return value
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

function normalizeFailureWorker(value: unknown): StageFailureWorker | null {
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text) return null
    if (text.startsWith('tmux ')) return { workerId: '', sessionName: '', attachCommand: text, workDir: '' }
    return { workerId: '', sessionName: text, attachCommand: `tmux attach -t ${shellQuote(text)}`, workDir: '' }
  }
  const item = objectOf(value)
  const sessionName = str(item.session_name ?? item.sessionName).trim()
  const rawAttach = item.attach_command ?? item.attachCommand
  const attachCommand = (Array.isArray(rawAttach) ? rawAttach.map((item) => shellQuote(str(item))).join(' ') : str(rawAttach)).trim()
    || (sessionName ? `tmux attach -t ${shellQuote(sessionName)}` : '')
  if (!sessionName && !attachCommand) return null
  return {
    workerId: str(item.worker_id ?? item.workerId).trim(),
    sessionName,
    attachCommand,
    workDir: str(item.work_dir ?? item.workDir).trim(),
  }
}

export function normalizeStageFailure(value: unknown): StageFailureSnapshot | null {
  const item = objectOf(value)
  if (Object.keys(item).length === 0) return null
  const workers = item.orphaned_workers ?? item.orphanedWorkers
  return {
    action: str(item.action).trim(),
    stageLabel: str(item.stage_label ?? item.stageLabel).trim(),
    status: str(item.status || 'failed').trim().toLowerCase(),
    source: str(item.source).trim().toLowerCase(),
    runnerId: str(item.runner_id ?? item.runnerId).trim(),
    stageSeq: num(item.stage_seq ?? item.stageSeq),
    message: str(item.message ?? item.error).trim(),
    failurePath: str(item.failure_path ?? item.failurePath ?? item.path).trim(),
    failureKind: str(item.failure_kind ?? item.failureKind ?? item.kind).trim(),
    orphanedWorkers: (Array.isArray(workers) ? workers : [])
      .map(normalizeFailureWorker)
      .filter((worker): worker is StageFailureWorker => worker !== null),
  }
}

export function normalizeFileSnapshot(value: unknown): FileSnapshot {
  const item = objectOf(value)
  return {
    label: str(item.label),
    path: str(item.path),
    exists: bool(item.exists),
    updatedAt: str(item.updated_at ?? item.updatedAt),
    summary: str(item.summary),
  }
}

export function normalizeWorkerSnapshot(value: unknown): WorkerSnapshot {
  const item = objectOf(value)
  const rawArtifactPaths = item.artifact_paths ?? item.artifactPaths
  const rawStateRevision = item.state_revision ?? item.stateRevision
  const stateRevision = (
    typeof rawStateRevision === 'number'
    || (typeof rawStateRevision === 'string' && rawStateRevision.trim() !== '')
  ) ? Number(rawStateRevision) : Number.NaN
  return {
    index: num(item.index) || undefined,
    workerId: str(item.worker_id ?? item.workerId),
    statePath: str(item.state_path ?? item.statePath),
    stateRevision: Number.isFinite(stateRevision) ? stateRevision : undefined,
    workDir: str(item.work_dir ?? item.workDir),
    sessionName: str(item.session_name ?? item.sessionName),
    status: str(item.status),
    resultStatus: str(item.result_status ?? item.resultStatus),
    workflowAction: str(item.workflow_action ?? item.workflowAction),
    workflowStage: str(item.workflow_stage ?? item.workflowStage),
    agentState: str(item.agent_state ?? item.agentState),
    healthStatus: str(item.health_status ?? item.healthStatus),
    currentTaskRuntimeStatus: str(item.current_task_runtime_status ?? item.currentTaskRuntimeStatus),
    dispatchState: str(item.dispatch_state ?? item.dispatchState),
    dispatchReason: str(item.dispatch_reason ?? item.dispatchReason),
    turnState: str(item.turn_state ?? item.turnState),
    tmuxControlStatus: str(item.tmux_control_status ?? item.tmuxControlStatus),
    tmuxControlError: str(item.tmux_control_error ?? item.tmuxControlError),
    tmuxUnavailableSince: str(
      item.tmux_control_unavailable_since
      ?? item.tmuxControlUnavailableSince
      ?? item.tmux_unavailable_since
      ?? item.tmuxUnavailableSince,
    ),
    stageRunnerId: str(item.stage_runner_id ?? item.stageRunnerId),
    orphanedAt: str(item.orphaned_at ?? item.orphanedAt),
    orphanedReason: str(item.orphaned_reason ?? item.orphanedReason),
    vendor: str(item.vendor),
    model: str(item.model),
    resolvedModel: str(item.resolved_model ?? item.resolvedModel),
    reasoningEffort: str(item.reasoning_effort ?? item.reasoningEffort),
    ponytailMode: item.ponytail_mode === undefined && item.ponytailMode === undefined
      ? undefined
      : str(item.ponytail_mode ?? item.ponytailMode),
    ponytailBundleVersion: item.ponytail_bundle_version === undefined && item.ponytailBundleVersion === undefined
      ? undefined
      : str(item.ponytail_bundle_version ?? item.ponytailBundleVersion),
    ponytailDelivery: item.ponytail_delivery === undefined && item.ponytailDelivery === undefined
      ? undefined
      : str(item.ponytail_delivery ?? item.ponytailDelivery),
    requirementsMode: item.requirements_mode === undefined && item.requirementsMode === undefined
      ? undefined
      : str(item.requirements_mode ?? item.requirementsMode),
    grillBundleCommit: item.grill_bundle_commit === undefined && item.grillBundleCommit === undefined
      ? undefined
      : str(item.grill_bundle_commit ?? item.grillBundleCommit),
    grillDelivery: item.grill_delivery === undefined && item.grillDelivery === undefined
      ? undefined
      : str(item.grill_delivery ?? item.grillDelivery),
    grillQuestionSeq: (() => {
      const raw = item.grill_question_seq ?? item.grillQuestionSeq
      if (raw === undefined || raw === null || raw === '') return undefined
      const parsed = Number(raw)
      return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined
    })(),
    retryCount: num(item.retry_count ?? item.retryCount),
    note: str(item.note),
    transcriptPath: str(item.transcript_path ?? item.transcriptPath),
    turnStatusPath: str(item.turn_status_path ?? item.turnStatusPath),
    questionPath: str(item.question_path ?? item.questionPath),
    answerPath: str(item.answer_path ?? item.answerPath),
    artifactPaths: Array.isArray(rawArtifactPaths) ? rawArtifactPaths.map(str) : [],
    sessionExists: item.session_exists === undefined && item.sessionExists === undefined ? undefined : bool(item.session_exists ?? item.sessionExists),
    lastHeartbeatAt: str(item.last_heartbeat_at ?? item.lastHeartbeatAt),
    updatedAt: str(item.updated_at ?? item.updatedAt),
  }
}

export function normalizeStageSnapshot(value: unknown): StageSnapshot {
  const item = objectOf(value)
  const milestones = Array.isArray(item.milestones)
    ? item.milestones.map((raw) => {
      const milestone = objectOf(raw)
      return {
        key: str(milestone.key),
        completed: bool(milestone.completed),
        tasks: Array.isArray(milestone.tasks)
          ? milestone.tasks.map((rawTask) => {
            const task = objectOf(rawTask)
            return { key: str(task.key), completed: bool(task.completed) }
          })
          : [],
      }
    })
    : []
  return {
    projectDir: str(item.project_dir ?? item.projectDir),
    requirementName: str(item.requirement_name ?? item.requirementName),
    files: Array.isArray(item.files) ? item.files.map(normalizeFileSnapshot) : [],
    workers: Array.isArray(item.workers) ? item.workers.map(normalizeWorkerSnapshot) : [],
    blockers: Array.isArray(item.blockers) ? item.blockers.map(str) : [],
    statusText: str(item.status_text ?? item.statusText),
    done: bool(item.done),
    milestones,
    currentMilestoneKey: str(item.current_milestone_key ?? item.currentMilestoneKey),
    allTasksCompleted: bool(item.all_tasks_completed ?? item.allTasksCompleted),
  }
}

export function normalizeControlSnapshot(value: unknown): ControlSnapshot {
  const item = objectOf(value)
  return {
    supported: item.supported === undefined ? true : bool(item.supported),
    controlId: str(item.control_id ?? item.controlId),
    runId: str(item.run_id ?? item.runId),
    runtimeDir: str(item.runtime_dir ?? item.runtimeDir),
    statusText: str(item.status_text ?? item.statusText),
    helpText: str(item.help_text ?? item.helpText),
    workers: Array.isArray(item.workers) ? item.workers.map(normalizeWorkerSnapshot) : [],
    done: bool(item.done),
    canSwitchRuns: bool(item.can_switch_runs ?? item.canSwitchRuns),
    finalSummary: str(item.final_summary ?? item.finalSummary),
    transitionText: str(item.transition_text ?? item.transitionText),
  }
}

export function normalizeHitlSnapshot(value: unknown): HitlSnapshot {
  const item = objectOf(value)
  const rawTargetPaths = item.target_paths ?? item.targetPaths
  return {
    pending: bool(item.pending),
    promptId: str(item.prompt_id ?? item.promptId),
    promptType: str(item.prompt_type ?? item.promptType),
    questionPath: str(item.question_path ?? item.questionPath),
    answerPath: str(item.answer_path ?? item.answerPath),
    summary: str(item.summary),
    attachCommand: str(item.attach_command ?? item.attachCommand),
    recoveryKind: str(item.recovery_kind ?? item.recoveryKind),
    reasonText: str(item.reason_text ?? item.reasonText),
    targetPaths: Array.isArray(rawTargetPaths) ? rawTargetPaths.map(str) : [],
  }
}

export function normalizePromptSnapshot(value: unknown): PromptSnapshot {
  const item = objectOf(value)
  const payload = objectOf(item.payload)
  const metadata = resolvePromptMetadata(item, payload)
  const rawQuestionSeq = item.question_seq ?? item.questionSeq ?? payload.question_seq ?? payload.questionSeq
  const parsedQuestionSeq = Number(rawQuestionSeq)
  return {
    pending: bool(item.pending),
    promptId: str(item.prompt_id ?? item.promptId),
    promptType: str(item.prompt_type ?? item.promptType),
    payload,
    interactionKind: metadata.interactionKind || undefined,
    questionIndex: metadata.questionIndex,
    recommendation: metadata.recommendation || undefined,
    reasonText: metadata.reasonText || undefined,
    ownerRunnerId: str(
      item.owner_runner_id ?? item.ownerRunnerId ?? payload.owner_runner_id ?? payload.ownerRunnerId,
    ) || undefined,
    questionSeq: Number.isSafeInteger(parsedQuestionSeq) && parsedQuestionSeq >= 0
      ? parsedQuestionSeq
      : undefined,
  }
}

export function normalizeArtifactsSnapshot(value: unknown): ArtifactsSnapshot {
  const item = objectOf(value)
  return {
    items: Array.isArray(item.items) ? item.items.map(artifact) : [],
  }
}

export function normalizeAppSnapshot(value: unknown): AppSnapshot {
  const item = objectOf(value)
  const runs = Array.isArray(item.available_runs) ? item.available_runs : Array.isArray(item.availableRuns) ? item.availableRuns : []
  const activeStage = str(item.active_stage ?? item.activeStage ?? 'idle')
  const activeStageStatus = str(item.active_stage_status ?? item.activeStageStatus)
  const activeStageSeq = num(item.active_stage_seq ?? item.activeStageSeq)
  const activeStageRunnerId = str(item.active_stage_runner_id ?? item.activeStageRunnerId ?? item.runner_id ?? item.runnerId)
  const activeStageSource = str(item.active_stage_source ?? item.activeStageSource ?? item.source)
  const activeStageLabel = str(item.active_stage_label ?? item.activeStageLabel ?? '等待中')
  const activeStageMessage = str(item.active_stage_message ?? item.activeStageMessage)
  const rawFailure = objectOf(item.active_stage_failure ?? item.activeStageFailure)
  const activeStageFailure = Object.keys(rawFailure).length > 0 ? normalizeStageFailure({
    ...rawFailure,
    action: rawFailure.action ?? activeStage,
    stage_label: rawFailure.stage_label ?? rawFailure.stageLabel ?? activeStageLabel,
    status: rawFailure.status ?? activeStageStatus,
    source: rawFailure.source ?? activeStageSource,
    runner_id: rawFailure.runner_id ?? rawFailure.runnerId ?? activeStageRunnerId,
    stage_seq: rawFailure.stage_seq ?? rawFailure.stageSeq ?? activeStageSeq,
  }) : null
  return {
    projectDir: str(item.project_dir ?? item.projectDir),
    requirementName: str(item.requirement_name ?? item.requirementName),
    currentAction: str(item.current_action ?? item.currentAction),
    activeRunId: str(item.active_run_id ?? item.activeRunId),
    activeStage,
    activeStageStatus,
    activeStageSeq,
    activeStageRunnerId,
    activeStageSource,
    activeStageLabel,
    activeStageMessage,
    activeStageFailure,
    pendingHitl: bool(item.pending_hitl ?? item.pendingHitl),
    pendingAttention: bool(item.pending_attention ?? item.pendingAttention),
    pendingAttentionReason: str(item.pending_attention_reason ?? item.pendingAttentionReason),
    pendingAttentionSince: str(item.pending_attention_since ?? item.pendingAttentionSince),
    graphify: normalizeGraphifyStatus(item.graphify),
    recentArtifacts: Array.isArray(item.recent_artifacts) ? item.recent_artifacts.map(artifact) : [],
    availableRuns: runs.map((run): RunOption => {
      const raw = objectOf(run)
      return {
        runId: str(raw.run_id ?? raw.runId),
        runtimeDir: str(raw.runtime_dir ?? raw.runtimeDir),
        projectDir: str(raw.project_dir ?? raw.projectDir),
        status: str(raw.status),
        updatedAt: str(raw.updated_at ?? raw.updatedAt),
        workerCount: num(raw.worker_count ?? raw.workerCount),
        failedCount: num(raw.failed_count ?? raw.failedCount),
      }
    }),
    capabilities: objectOf(item.capabilities),
  }
}

export function normalizeSnapshotsPayload(value: unknown): SnapshotsPayload {
  const item = objectOf(value)
  const rawStages = objectOf(item.stages)
  const stages = Object.fromEntries(
    STAGE_ROUTES.map((route) => [route, normalizeStageSnapshot(rawStages[route])]),
  ) as Record<StageRoute, StageSnapshot>
  return {
    app: normalizeAppSnapshot(item.app),
    stages,
    control: normalizeControlSnapshot(item.control),
    hitl: normalizeHitlSnapshot(item.hitl),
    artifacts: normalizeArtifactsSnapshot(item.artifacts),
    prompt: normalizePromptSnapshot(item.prompt),
  }
}

export function normalizeBootstrapPayload(value: unknown): BootstrapPayload {
  const item = objectOf(value)
  return {
    protocolVersion: str(item.protocol_version ?? item.protocolVersion),
    pythonPath: str(item.python_path ?? item.pythonPath),
    routes: Array.isArray(item.routes) ? item.routes.map(str) as BootstrapPayload['routes'] : [],
    commands: Array.isArray(item.commands) ? item.commands.map(str) : [],
    capabilities: objectOf(item.capabilities),
    snapshots: normalizeSnapshotsPayload(item.snapshots),
  }
}

export function normalizeAgentCatalog(value: unknown): AgentCatalog {
  const item = objectOf(value)
  const vendors = Array.isArray(item.vendors) ? item.vendors.map((rawVendor): AgentVendorOption => {
    const vendor = objectOf(rawVendor)
    const models = Array.isArray(vendor.models) ? vendor.models.map((rawModel): AgentModelOption => {
      const model = objectOf(rawModel)
      const rawEfforts = Array.isArray(model.efforts) ? model.efforts.map(str).filter(Boolean) : []
      return {
        modelId: str(model.model_id ?? model.modelId),
        displayName: str(model.display_name ?? model.displayName ?? model.model_id ?? model.modelId),
        sourceKind: str(model.source_kind ?? model.sourceKind),
        confidence: str(model.confidence),
        synthetic: bool(model.synthetic),
        efforts: rawEfforts.length > 0 ? rawEfforts : ['high'],
        defaultEffort: str(model.default_effort ?? model.defaultEffort) || (rawEfforts.includes('high') ? 'high' : rawEfforts[0] || 'high'),
      }
    }) : []
    return {
      vendorId: str(vendor.vendor_id ?? vendor.vendorId),
      installed: bool(vendor.installed),
      scanStatus: str(vendor.scan_status ?? vendor.scanStatus),
      sourceKind: str(vendor.source_kind ?? vendor.sourceKind),
      confidence: str(vendor.confidence),
      defaultModel: str(vendor.default_model ?? vendor.defaultModel),
      models,
    }
  }) : []
  return {
    schemaVersion: str(item.schema_version ?? item.schemaVersion),
    generatedAt: str(item.generated_at ?? item.generatedAt),
    vendors,
  }
}

export function normalizeFilePreview(value: unknown): FilePreview {
  const item = objectOf(value)
  return {
    path: str(item.path),
    size: num(item.size),
    updatedAt: str(item.updated_at ?? item.updatedAt),
    truncated: bool(item.truncated),
    text: str(item.text),
  }
}

export function normalizeRequirementsList(value: unknown): RequirementsList {
  const item = objectOf(value)
  const requirements = Array.isArray(item.requirements) ? item.requirements.map((raw): RequirementOption => {
    const requirement = objectOf(raw)
    return {
      name: str(requirement.name),
      path: str(requirement.path),
    }
  }).filter((requirement) => requirement.name) : []
  return {
    schemaVersion: str(item.schema_version ?? item.schemaVersion),
    projectDir: str(item.project_dir ?? item.projectDir),
    requirements,
  }
}
