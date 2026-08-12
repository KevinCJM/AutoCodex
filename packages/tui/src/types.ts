export type FileSnapshot = {
  label: string
  path: string
  exists: boolean
  updatedAt: string
  summary: string
}

export type WorkerSnapshot = {
  index?: number
  workerId?: string
  statePath?: string
  stateRevision?: number
  workDir: string
  sessionName: string
  status: string
  resultStatus?: string
  workflowAction?: string
  workflowStage: string
  agentState: string
  healthStatus: string
  currentTaskRuntimeStatus?: string
  dispatchState?: string
  dispatchReason?: string
  turnState?: string
  currentTurnId?: string
  tmuxControlStatus?: string
  tmuxControlError?: string
  tmuxUnavailableSince?: string
  stageRunnerId?: string
  orphanedAt?: string
  orphanedReason?: string
  vendor?: string
  model?: string
  resolvedModel?: string
  reasoningEffort?: string
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
  lastHeartbeatAt?: string
  updatedAt?: string
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

export type RoutingSnapshot = {
  projectDir: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
  statusText: string
  done: boolean
}

export type RequirementsSnapshot = {
  projectDir: string
  requirementName: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
}

export type ReviewSnapshot = {
  projectDir: string
  requirementName: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
  blockers: string[]
}

export type DesignSnapshot = {
  projectDir: string
  requirementName: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
  blockers: string[]
}

export type TaskSplitSnapshot = {
  projectDir: string
  requirementName: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
  blockers: string[]
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

export type DevelopmentSnapshot = {
  projectDir: string
  requirementName: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
  blockers: string[]
  milestones: DevelopmentMilestone[]
  currentMilestoneKey: string
  allTasksCompleted: boolean
}

export type OverallReviewSnapshot = {
  projectDir: string
  requirementName: string
  files: FileSnapshot[]
  workers: WorkerSnapshot[]
  blockers: string[]
}

export type HitlSnapshot = {
  pending: boolean
  promptId?: string
  promptType?: string
  questionPath: string
  answerPath: string
  summary: string
  attachCommand: string
  recoveryKind?: string
  reasonText?: string
  targetPaths?: string[]
}

export type ArtifactItem = {
  path: string
  updatedAt: string
  summary: string
}

export type HomeAgentItem = {
  source: 'control' | 'routing' | 'requirements' | 'review' | 'design' | 'task-split' | 'development' | 'overall-review'
  sessionName: string
  healthStatus: string
  agentState: string
  agentConfigLabel: string
  attachCommand: string
  workDir: string
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
  activeStageMessage?: string
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
