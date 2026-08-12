export type StageRoute =
  | 'routing'
  | 'requirements'
  | 'review'
  | 'design'
  | 'task-split'
  | 'development'
  | 'overall-review'

export type RouteName = 'home' | StageRoute | 'control'

export type FileSnapshot = {
  label: string
  path: string
  exists: boolean
  updatedAt: string
  summary: string
}

export type WorkerSnapshot = {
  index?: number
  workerId: string
  statePath: string
  stateRevision?: number
  workDir: string
  sessionName: string
  status: string
  resultStatus: string
  workflowAction: string
  workflowStage: string
  agentState: string
  healthStatus: string
  currentTaskRuntimeStatus: string
  dispatchState: string
  dispatchReason: string
  turnState: string
  currentTurnId?: string
  tmuxControlStatus: string
  tmuxControlError: string
  tmuxUnavailableSince: string
  stageRunnerId: string
  orphanedAt: string
  orphanedReason: string
  vendor: string
  model: string
  resolvedModel: string
  reasoningEffort: string
  ponytailMode?: string
  ponytailBundleVersion?: string
  ponytailDelivery?: string
  requirementsMode?: string
  requirementsBehavior?: string
  grillBundleCommit?: string
  grillDelivery?: string
  grillQuestionSeq?: number
  codegraphMode?: string
  codegraphAvailable?: boolean
  codegraphHintDelivery?: string
  retryCount: number
  note: string
  transcriptPath: string
  turnStatusPath: string
  questionPath: string
  answerPath: string
  artifactPaths: string[]
  sessionExists?: boolean
  lastHeartbeatAt: string
  updatedAt: string
}

export type ControlSnapshot = {
  supported: boolean
  controlId: string
  runId: string
  runtimeDir: string
  statusText: string
  helpText: string
  workers: WorkerSnapshot[]
  done: boolean
  canSwitchRuns: boolean
  finalSummary: string
  transitionText: string
}

export type StageSnapshot = {
  projectDir: string
  requirementName: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
  blockers: string[]
  statusText: string
  done: boolean
  milestones: DevelopmentMilestone[]
  currentMilestoneKey: string
  allTasksCompleted: boolean
}

export type DevelopmentTaskItem = {
  key: string
  completed: boolean
}

export type DevelopmentMilestone = {
  key: string
  completed: boolean
  tasks: DevelopmentTaskItem[]
}

export type HitlSnapshot = {
  pending: boolean
  promptId: string
  promptType: string
  questionPath: string
  answerPath: string
  summary: string
  attachCommand: string
  recoveryKind: string
  reasonText: string
  targetPaths: string[]
}

export type HomeAgentItem = {
  source: 'control' | StageRoute
  workerId: string
  sessionName: string
  healthStatus: string
  agentState: string
  turnState: string
  agentConfigLabel: string
  attachCommand: string
  workDir: string
}

export type ArtifactItem = {
  path: string
  updatedAt: string
  summary: string
}

export type ArtifactsSnapshot = {
  items: ArtifactItem[]
}

export type RunOption = {
  runId: string
  runtimeDir: string
  projectDir: string
  status: string
  updatedAt: string
  workerCount: number
  failedCount: number
}

export type StageFailureWorker = {
  workerId: string
  sessionName: string
  attachCommand: string
  workDir: string
}

export type StageFailureSnapshot = {
  action: string
  stageLabel: string
  status: string
  source: string
  runnerId: string
  stageSeq: number
  message: string
  failurePath: string
  failureKind: string
  orphanedWorkers: StageFailureWorker[]
}

export type CodeGraphStatus = {
  mode: string
  state: string
  version: string
  freshness: string
  initialized: boolean
  lastIndexed: string
  fileCount: number
  nodeCount: number
  edgeCount: number
  pendingChanges: { added: number; modified: number; removed: number }
  indexState: string
  pendingRefs: number
  reindexRecommended: boolean
  worktreeMismatch: boolean
  lastError: string
}

export type AppSnapshot = {
  projectDir: string
  requirementName: string
  currentAction: string
  activeRunId: string
  activeStage: string
  activeStageStatus: string
  activeStageSeq: number
  activeStageRunnerId: string
  activeStageSource: string
  activeStageLabel: string
  activeStageMessage: string
  activeStageFailure: StageFailureSnapshot | null
  pendingHitl: boolean
  pendingAttention: boolean
  pendingAttentionReason: string
  pendingAttentionSince: string
  codegraph?: CodeGraphStatus
  recentArtifacts: ArtifactItem[]
  availableRuns: RunOption[]
  capabilities: Record<string, unknown>
}

export type PromptSnapshot = {
  pending: boolean
  promptId: string
  promptType: 'text' | 'select' | 'multiline' | string
  payload: Record<string, unknown>
  interactionKind?: string
  questionIndex?: number
  recommendation?: string
  reasonText?: string
  ownerRunnerId?: string
  questionSeq?: number
}

export type SnapshotsPayload = {
  app: AppSnapshot
  stages: Record<StageRoute, StageSnapshot>
  control: ControlSnapshot
  hitl: HitlSnapshot
  artifacts: ArtifactsSnapshot
  prompt: PromptSnapshot
}

export type BootstrapPayload = {
  protocolVersion: string
  pythonPath: string
  routes: RouteName[]
  commands: string[]
  capabilities: Record<string, unknown>
  snapshots: SnapshotsPayload
}

export type AgentModelOption = {
  modelId: string
  displayName: string
  sourceKind: string
  confidence: string
  synthetic: boolean
  efforts: string[]
  defaultEffort: string
}

export type AgentVendorOption = {
  vendorId: string
  installed: boolean
  scanStatus: string
  sourceKind: string
  confidence: string
  defaultModel: string
  models: AgentModelOption[]
}

export type AgentCatalog = {
  schemaVersion: string
  generatedAt: string
  vendors: AgentVendorOption[]
}

export type RequirementOption = {
  name: string
  path: string
}

export type RequirementsList = {
  schemaVersion: string
  projectDir: string
  requirements: RequirementOption[]
}

export type BridgeEvent = {
  type: string
  payload: Record<string, unknown>
}

export type LogKind = 'stage' | 'summary' | 'runtime' | 'warning' | 'error' | 'hitl' | 'plain'

export type LogEntry = {
  id: number
  kind: LogKind
  title: string
  lines: string[]
  createdAt: string
}

export type FilePreview = {
  path: string
  size: number
  updatedAt: string
  truncated: boolean
  text: string
}
