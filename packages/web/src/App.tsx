import { Activity, AlertTriangle, Copy, FileText, GitBranch, Home, MoreHorizontal, RefreshCcw, RotateCcw, Send, Square, Terminal, X, Zap } from 'lucide-solid'
import { For, Match, Show, Switch, createEffect, createMemo, createSignal, onCleanup, onMount } from 'solid-js'
import type { JSX } from 'solid-js'
import {
  connectBridgeEvents,
  getBootstrap,
  getFilePreview,
  getPrompt,
  getSnapshots,
  postBridgeRequest,
  submitPromptResponse,
} from './api/client'
import { appendLog, classifyLog } from './domain/logs'
import {
  EMPTY_APP,
  EMPTY_CONTROL,
  EMPTY_HITL,
  EMPTY_PROMPT,
  EMPTY_STAGE,
  normalizeAppSnapshot,
  normalizeArtifactsSnapshot,
  normalizeControlSnapshot,
  normalizeHitlSnapshot,
  normalizePromptSnapshot,
  normalizeStageSnapshot,
} from './domain/normalize'
import { STAGE_LABELS, STAGE_ROUTES, routeLabel, stageRouteForAction } from './domain/stages'
import type { AppSnapshot, ArtifactsSnapshot, BridgeEvent, ControlSnapshot, FilePreview, HitlSnapshot, LogEntry, PromptSnapshot, RunOption, SnapshotsPayload, StageRoute, StageSnapshot, WorkerSnapshot } from './domain/types'

type AppTab = 'home' | 'stages' | 'files' | 'logs'
type SheetKind = 'advanced' | 'preview' | null
type ChoiceOption = { value: string; label: string }
const PROMPT_BACK_VALUE = '__tmux_back__'

function emptySnapshots(): SnapshotsPayload {
  return {
    app: EMPTY_APP,
    stages: Object.fromEntries(STAGE_ROUTES.map((route) => [route, { ...EMPTY_STAGE }])) as Record<StageRoute, StageSnapshot>,
    control: EMPTY_CONTROL,
    hitl: EMPTY_HITL,
    artifacts: { items: [] },
    prompt: EMPTY_PROMPT,
  }
}

function statusClass(value: string): string {
  const text = String(value || '').toLowerCase()
  if (text.includes('fail') || text.includes('dead') || text.includes('error')) return 'danger'
  if (text.includes('busy') || text.includes('running') || text.includes('pending') || text.includes('start')) return 'active'
  if (text.includes('complete') || text.includes('ready') || text.includes('alive')) return 'good'
  return 'muted'
}

async function copyText(value: string) {
  if (!value.trim()) return
  await navigator.clipboard?.writeText(value)
}

function AppHeader(props: {
  app: AppSnapshot
  connection: string
  progress: string
  onRefresh: () => void
  onMore: () => void
}) {
  return (
    <header class="app-header">
      <div class="app-brand">
        <Zap size={18} />
        <div>
          <h1>TMUX</h1>
          <p>{props.app.activeStageLabel || '等待中'}</p>
        </div>
      </div>
      <div class="header-actions">
        <span class={`connection-dot ${props.connection === 'online' ? 'good-bg' : 'danger-bg'}`} title={props.connection} />
        <button class="icon-button" aria-label="refresh" title="refresh" onClick={props.onRefresh}>
          <RefreshCcw size={17} />
        </button>
        <button class="icon-button" aria-label="more" title="more" onClick={props.onMore}>
          <MoreHorizontal size={18} />
        </button>
      </div>
      <Show when={props.progress}>
        <div class="progress-line"><Activity size={13} />{props.progress}</div>
      </Show>
    </header>
  )
}

function BottomTabs(props: { activeTab: AppTab; onSelect: (tab: AppTab) => void }) {
  const tabs: Array<{ id: AppTab; label: string; icon: typeof Home }> = [
    { id: 'home', label: '当前', icon: Home },
    { id: 'stages', label: '阶段', icon: GitBranch },
    { id: 'files', label: '文件', icon: FileText },
    { id: 'logs', label: '日志', icon: Terminal },
  ]
  return (
    <nav class="bottom-tabs" aria-label="main tabs">
      <For each={tabs}>
        {(tab) => {
          const Icon = tab.icon
          return (
            <button class={`tab-button ${props.activeTab === tab.id ? 'selected' : ''}`} onClick={() => props.onSelect(tab.id)}>
              <Icon size={18} />
              <span>{tab.label}</span>
            </button>
          )
        }}
      </For>
    </nav>
  )
}

function StatLine(props: { label: string; value: string | number; tone?: string }) {
  return (
    <div class="stat-line">
      <span>{props.label}</span>
      <strong class={props.tone || ''}>{props.value}</strong>
    </div>
  )
}

function PathButton(props: { path: string; label?: string; onPreview: (path: string) => void }) {
  return (
    <button class="path-button" title={props.path} onClick={() => props.onPreview(props.path)}>
      <FileText size={15} />
      <span>{props.label || props.path}</span>
    </button>
  )
}

function ChoiceButtons(props: { options: ChoiceOption[]; value: string; onChange: (value: string) => void; class?: string; ariaLabel?: string; disabled?: boolean }) {
  const groupName = `choice-${Math.random().toString(36).slice(2)}`
  return (
    <div class={`choice-list ${props.class || ''}`} role="radiogroup" aria-label={props.ariaLabel}>
      <For each={props.options}>
        {(option) => (
          <label
            class={`choice-button radio-choice ${props.value === option.value ? 'selected' : ''}`}
            title={option.value || option.label}
          >
            <input
              class="choice-radio"
              type="radio"
              name={groupName}
              value={option.value}
              checked={props.value === option.value}
              disabled={props.disabled}
              onChange={() => props.onChange(option.value)}
            />
            <span>{option.label}</span>
          </label>
        )}
      </For>
    </div>
  )
}

function PromptCard(props: { prompt: PromptSnapshot; hitl: HitlSnapshot; onSubmit: (value: unknown) => Promise<void>; onPreview: (path: string) => void }) {
  const [draft, setDraft] = createSignal('')
  const [error, setError] = createSignal('')
  const [submitting, setSubmitting] = createSignal(false)
  const promptType = createMemo(() => String(props.prompt.promptType || 'text'))
  const isSelectPrompt = createMemo(() => promptType() === 'select' || promptType() === 'confirm')
  const selectOptions = createMemo<ChoiceOption[]>(() => {
    const rawOptions = Array.isArray(props.prompt.payload.options) ? props.prompt.payload.options : []
    const options = rawOptions.map((option) => {
      if (typeof option === 'string') return { value: option, label: option }
      const record = option as Record<string, unknown>
      const value = String(record.value ?? record.id ?? record.key ?? record.label ?? '')
      const label = String(record.label ?? record.title ?? value)
      return { value, label }
    }).filter((option) => option.label)
    if (options.length > 0 || promptType() !== 'confirm') return options
    return [
      { value: 'yes', label: '是' },
      { value: 'no', label: '否' },
    ]
  })
  const isBackOption = (option: ChoiceOption) => {
    const value = option.value.trim().toLowerCase()
    const label = option.label.trim().toLowerCase()
    return value === PROMPT_BACK_VALUE || value === '__back__' || value === 'back' || value === 'previous' || value === 'prev' || label === '上一步' || label.includes('返回')
  }
  const backOption = createMemo(() => selectOptions().find(isBackOption))
  const visibleSelectOptions = createMemo(() => selectOptions().filter((option) => !isBackOption(option)))
  const title = createMemo(() => String(props.prompt.payload.title ?? props.prompt.payload.prompt_text ?? '待处理输入'))
  const backendError = createMemo(() => String(
    props.prompt.payload.error_message
    ?? props.prompt.payload.errorMessage
    ?? props.prompt.payload.validation_error
    ?? props.prompt.payload.validationError
    ?? '',
  ).trim())
  const previewPath = createMemo(() => String(props.prompt.payload.preview_path ?? props.prompt.payload.question_path ?? props.hitl.questionPath ?? '').trim())
  const fieldId = createMemo(() => `prompt-input-${props.prompt.promptId || 'pending'}`)
  const backendBackAllowed = createMemo(() => Boolean(props.prompt.payload.can_go_back ?? props.prompt.payload.canGoBack ?? props.prompt.payload.allow_back ?? props.prompt.payload.allowBack))
  const canGoBack = createMemo(() => Boolean(backOption() || backendBackAllowed()))
  const initialValue = createMemo(() => {
    const rawDefault = props.prompt.payload.default_value ?? props.prompt.payload.default
    if (promptType() === 'confirm' && typeof rawDefault === 'boolean') return rawDefault ? 'yes' : 'no'
    return String(rawDefault ?? (isSelectPrompt() ? visibleSelectOptions()[0]?.value ?? selectOptions()[0]?.value ?? '' : ''))
  })
  let previousPromptKey = ''
  const changeChoice = (value: string) => {
    if (submitting()) return
    setDraft(value)
  }
  const sendValue = async (value: unknown) => {
    if (submitting()) return
    setError('')
    setSubmitting(true)
    try {
      await props.onSubmit(value)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSubmitting(false)
    }
  }
  const goBack = () => {
    const option = backOption()
    if (option) {
      void sendValue(option.value)
      return
    }
    if (backendBackAllowed()) {
      void sendValue(PROMPT_BACK_VALUE)
      return
    }
  }
  const submitValue = () => {
    if (isSelectPrompt()) return draft()
    const field = document.getElementById(fieldId()) as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null
    return field?.value ?? draft()
  }
  const submit = () => {
    const value = submitValue()
    if (promptType() === 'multiline' && !String(value).trim()) {
      setError(String(props.prompt.payload.empty_retry_message ?? '输入不能为空，请重试。'))
      return
    }
    void sendValue(value)
  }

  createEffect(() => {
    const promptKey = `${props.prompt.promptId}:${props.prompt.promptType}`
    if (promptKey === previousPromptKey) return
    previousPromptKey = promptKey
    setError('')
    setSubmitting(false)
    setDraft(initialValue())
  })

  return (
    <section class={`hero-card prompt-card ${submitting() ? 'is-processing' : ''}`}>
      <div class="card-kicker">需要你输入</div>
      <h2>{title()}</h2>
      <Show when={!isSelectPrompt()}>
        <label class="sr-only" for={fieldId()}>{title()}</label>
      </Show>
      <Switch>
        <Match when={isSelectPrompt()}>
          <ChoiceButtons options={visibleSelectOptions()} value={draft()} onChange={changeChoice} ariaLabel={title()} disabled={submitting()} />
        </Match>
        <Match when={promptType() === 'multiline'}>
          <textarea id={fieldId()} rows={7} value={draft()} disabled={submitting()} onInput={(event) => setDraft(event.currentTarget.value)} />
        </Match>
        <Match when={true}>
          <input id={fieldId()} value={draft()} disabled={submitting()} onInput={(event) => setDraft(event.currentTarget.value)} />
        </Match>
      </Switch>
      <Show when={submitting()}>
        <div class="prompt-processing" role="status" aria-live="polite">
          <span class="mini-spinner" />
          <span>处理中</span>
        </div>
      </Show>
      <Show when={error() || backendError()}><p class="form-error">{error() || backendError()}</p></Show>
      <div class="primary-actions">
        <Show when={canGoBack()}>
          <button class="ghost-button" disabled={submitting()} onClick={goBack}>上一步</button>
        </Show>
        <button class="primary-button" disabled={submitting()} onClick={submit}><Send size={16} />提交</button>
        <Show when={previewPath()}><button class="ghost-button" disabled={submitting()} onClick={() => props.onPreview(previewPath())}><FileText size={16} />预览</button></Show>
      </div>
    </section>
  )
}

function CurrentCard(props: {
  app: AppSnapshot
  stage: StageSnapshot
  artifacts: ArtifactsSnapshot
}) {
  const workerCount = () => props.stage.workers.length
  const requirement = () => props.stage.requirementName || props.app.requirementName
  return (
    <section class="hero-card">
      <div class="card-kicker">当前阶段</div>
      <h2>{props.app.activeStageLabel || routeLabel(stageRouteForAction(props.app.activeStage)) || '等待中'}</h2>
      <Show when={requirement()}>
        <p class="hero-copy">{requirement()}</p>
      </Show>
      <div class="summary-strip">
        <StatLine label="智能体" value={workerCount()} tone={workerCount() > 0 ? 'active' : 'muted'} />
        <StatLine label="文件" value={props.artifacts.items.length} />
      </div>
    </section>
  )
}

function StartWorkflowCard(props: { busy: boolean; promptPending: boolean; onStart: () => Promise<void> }) {
  const [starting, setStarting] = createSignal(false)
  const [error, setError] = createSignal('')
  const disabled = createMemo(() => starting() || props.busy || props.promptPending)
  const statusText = createMemo(() => {
    if (props.promptPending) return '等待你完成当前输入'
    if (props.busy) return '运行中，系统会在需要你输入时显示问题'
    return '由后端按 TUI 顺序逐步询问参数'
  })
  const start = async () => {
    if (disabled()) return
    setError('')
    setStarting(true)
    try {
      await props.onStart()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setStarting(false)
    }
  }

  return (
    <section class="app-card start-card">
      <div class="view-title">
        <span>启动</span>
        <strong>{statusText()}</strong>
      </div>
      <Show when={error()}><p class="form-error">{error()}</p></Show>
      <button class="primary-button start-button" disabled={disabled()} onClick={() => void start()}>
        <Zap size={16} />启动
      </button>
    </section>
  )
}

function HomeView(props: {
  snapshots: SnapshotsPayload
  progress: string
  onPromptSubmit: (value: unknown) => Promise<void>
  onStartWorkflow: () => Promise<void>
  onPreview: (path: string) => void
}) {
  const app = () => props.snapshots.app
  const stageRoute = () => stageRouteForAction(app().activeStage) || 'routing'
  const stage = () => props.snapshots.stages[stageRoute() as StageRoute] || EMPTY_STAGE
  const busy = createMemo(() => Boolean(props.progress || (app().activeStage && app().activeStage !== 'idle') || props.snapshots.control.workers.length > 0))
  return (
    <div class="view-stack home-view">
      <Show when={props.snapshots.prompt.pending}>
        <PromptCard prompt={props.snapshots.prompt} hitl={props.snapshots.hitl} onSubmit={props.onPromptSubmit} onPreview={props.onPreview} />
      </Show>
      <CurrentCard app={app()} stage={stage()} artifacts={props.snapshots.artifacts} />
      <StartWorkflowCard
        busy={busy()}
        promptPending={props.snapshots.prompt.pending}
        onStart={props.onStartWorkflow}
      />
      <section class="app-card details-card">
        <div class="card-row">
          <span>项目</span>
          <strong>{app().projectDir || 'project unset'}</strong>
        </div>
        <Show when={app().activeRunId}>
          <div class="card-row">
            <span>Run</span>
            <strong>{app().activeRunId}</strong>
          </div>
        </Show>
        <Show when={props.progress}>
          <div class="card-row">
            <span>进度</span>
            <strong class="active">{props.progress}</strong>
          </div>
        </Show>
      </section>
    </div>
  )
}

function WorkerList(props: { workers: WorkerSnapshot[]; onPreview: (path: string) => void }) {
  return (
    <div class="worker-list">
      <For each={props.workers}>
        {(worker, index) => (
          <article class="worker-card">
            <div>
              <button class="link-button" onClick={() => void copyText(`tmux attach -t ${worker.sessionName}`)}>
                <Copy size={14} />
                <span>{worker.sessionName || `worker-${index() + 1}`}</span>
              </button>
              <p>{worker.workflowStage || worker.currentTaskRuntimeStatus || 'running'}</p>
            </div>
            <div class="status-pills">
              <span class={`pill ${statusClass(worker.status || worker.agentState)}`}>{worker.status || worker.agentState || 'unknown'}</span>
              <span class={`pill ${statusClass(worker.healthStatus)}`}>{worker.healthStatus || '-'}</span>
            </div>
            <div class="file-actions">
              <Show when={worker.transcriptPath}><PathButton path={worker.transcriptPath} label="transcript" onPreview={props.onPreview} /></Show>
              <Show when={worker.questionPath}><PathButton path={worker.questionPath} label="question" onPreview={props.onPreview} /></Show>
              <Show when={worker.turnStatusPath}><PathButton path={worker.turnStatusPath} label="turn" onPreview={props.onPreview} /></Show>
            </div>
          </article>
        )}
      </For>
    </div>
  )
}

function StageFiles(props: { stage: StageSnapshot; onPreview: (path: string) => void }) {
  return (
    <Show when={props.stage.files.length > 0}>
      <div class="file-list">
        <For each={props.stage.files}>
          {(file) => (
            <button class={`file-card ${file.exists ? '' : 'missing'}`} title={file.path} onClick={() => props.onPreview(file.path)}>
              <FileText size={17} />
              <span>{file.label || file.path.split('/').at(-1)}</span>
              <small>{file.exists ? file.summary || file.updatedAt : 'missing'}</small>
            </button>
          )}
        </For>
      </div>
    </Show>
  )
}

function StagesView(props: {
  snapshots: SnapshotsPayload
  selectedStage: StageRoute
  onSelectStage: (route: StageRoute) => void
  onPreview: (path: string) => void
}) {
  const activeRoute = () => stageRouteForAction(props.snapshots.app.activeStage)
  const selected = () => props.snapshots.stages[props.selectedStage] || EMPTY_STAGE
  return (
    <div class="view-stack">
      <section class="app-card">
        <div class="view-title">
          <span>Timeline</span>
          <strong>{STAGE_LABELS[props.selectedStage]}</strong>
        </div>
        <div class="stage-list">
          <For each={STAGE_ROUTES}>
            {(route, index) => {
              const stage = () => props.snapshots.stages[route] || EMPTY_STAGE
              return (
                <button class={`stage-item ${props.selectedStage === route ? 'selected' : ''} ${activeRoute() === route ? 'current' : ''}`} onClick={() => props.onSelectStage(route)}>
                  <span class="stage-index">{index() + 1}</span>
                  <span>{STAGE_LABELS[route]}</span>
                  <small>{stage().done ? 'done' : activeRoute() === route ? 'current' : `${stage().workers.length} agents`}</small>
                </button>
              )
            }}
          </For>
        </div>
      </section>
      <section class="app-card">
        <div class="view-title">
          <span>Stage detail</span>
          <strong>{selected().requirementName || props.snapshots.app.requirementName || 'unset'}</strong>
        </div>
        <Show when={selected().blockers.length > 0}>
          <div class="blockers">
            <For each={selected().blockers}>{(item) => <span><AlertTriangle size={14} />{item}</span>}</For>
          </div>
        </Show>
        <Show when={selected().milestones.length > 0}>
          <div class="milestone-list">
            <For each={selected().milestones}>
              {(milestone) => (
                <div class={`milestone ${milestone.key === selected().currentMilestoneKey ? 'active-milestone' : ''}`}>
                  <strong>{milestone.completed ? 'done' : 'todo'} · {milestone.key}</strong>
                  <For each={milestone.tasks}>{(task) => <span>{task.completed ? '✓' : '·'} {task.key}</span>}</For>
                </div>
              )}
            </For>
          </div>
        </Show>
        <WorkerList workers={selected().workers} onPreview={props.onPreview} />
        <StageFiles stage={selected()} onPreview={props.onPreview} />
      </section>
    </div>
  )
}

function FilesView(props: {
  snapshots: SnapshotsPayload
  selectedStage: StageRoute
  onPreview: (path: string) => void
}) {
  const stage = () => props.snapshots.stages[props.selectedStage] || EMPTY_STAGE
  return (
    <div class="view-stack">
      <section class="app-card">
        <div class="view-title">
          <span>Artifacts</span>
          <strong>{props.snapshots.artifacts.items.length}</strong>
        </div>
        <div class="file-list">
          <For each={props.snapshots.artifacts.items}>
            {(item) => (
              <button class="file-card" title={item.path} onClick={() => props.onPreview(item.path)}>
                <FileText size={17} />
                <span>{item.summary || item.path.split('/').at(-1)}</span>
                <small>{item.updatedAt || item.path}</small>
              </button>
            )}
          </For>
          <Show when={props.snapshots.artifacts.items.length === 0}>
            <p class="empty-state">还没有产物</p>
          </Show>
        </div>
      </section>
      <section class="app-card">
        <div class="view-title">
          <span>Stage files</span>
          <strong>{STAGE_LABELS[props.selectedStage]}</strong>
        </div>
        <StageFiles stage={stage()} onPreview={props.onPreview} />
        <Show when={stage().files.length === 0}>
          <p class="empty-state">当前阶段暂无文件</p>
        </Show>
      </section>
    </div>
  )
}

function LogsView(props: { logs: LogEntry[]; filter: string; onFilter: (value: string) => void }) {
  const filteredLogs = createMemo(() => props.logs.filter((entry) => props.filter === 'all' || entry.kind === props.filter))
  const filters = [
    { value: 'all', label: 'all' },
    { value: 'stage', label: 'stage' },
    { value: 'hitl', label: 'hitl' },
    { value: 'error', label: 'error' },
    { value: 'plain', label: 'plain' },
  ]
  return (
    <section class="app-card log-view">
      <div class="view-title">
        <span>Terminal</span>
        <ChoiceButtons options={filters} value={props.filter} onChange={props.onFilter} class="compact-choices" ariaLabel="日志过滤" />
      </div>
      <div class="log-list">
        <For each={filteredLogs()}>
          {(entry) => (
            <article class={`log-entry ${entry.kind}`}>
              <span>{entry.title}</span>
              <For each={entry.lines}>{(line) => <code>{line || ' '}</code>}</For>
            </article>
          )}
        </For>
        <Show when={filteredLogs().length === 0}>
          <p class="empty-state">no logs</p>
        </Show>
      </div>
    </section>
  )
}

function AdvancedPanel(props: {
  snapshots: SnapshotsPayload
  selectedControlIndex: number
  onControlSelect: (index: number) => void
  onControlAction: (action: 'attach' | 'detach' | 'restart' | 'retry' | 'kill') => void
  onRefresh: () => void
  onResumeRun: (runId: string) => void
  resumeRuns: RunOption[]
  onOpenResume: () => void
}) {
  const selectedWorker = createMemo(() => props.snapshots.control.workers[props.selectedControlIndex])
  const workerOptions = createMemo(() => props.snapshots.control.workers.map((item, index) => ({
    value: String(index),
    label: `${item.index || index + 1} · ${item.sessionName}`,
  })))
  return (
    <div class="advanced-panel">
      <section>
        <h3>Control Worker</h3>
        <Show when={selectedWorker()} fallback={<p class="empty-state">no control worker</p>}>
          {(worker) => (
            <>
              <ChoiceButtons options={workerOptions()} value={String(props.selectedControlIndex)} onChange={(value) => props.onControlSelect(Number(value))} ariaLabel="选择 control worker" />
              <p class="mono-line">{worker().workDir || worker().note}</p>
              <div class="button-grid">
                <button onClick={() => props.onControlAction('attach')}><Terminal size={15} />attach</button>
                <button onClick={() => props.onControlAction('restart')}><RotateCcw size={15} />restart</button>
                <button onClick={() => props.onControlAction('retry')}><RefreshCcw size={15} />retry</button>
                <button onClick={() => props.onControlAction('detach')}><Square size={15} />detach</button>
                <button class="danger-button" onClick={() => props.onControlAction('kill')}><Square size={15} />kill</button>
              </div>
            </>
          )}
        </Show>
        <div class="button-row">
          <button onClick={props.onOpenResume}><GitBranch size={15} />runs</button>
          <button onClick={props.onRefresh}><RefreshCcw size={15} />refresh</button>
        </div>
        <Show when={props.resumeRuns.length > 0}>
          <div class="choice-list run-choice-list" role="group" aria-label="选择 run">
            <For each={props.resumeRuns}>
              {(run) => (
                <button class="choice-button" onClick={() => props.onResumeRun(run.runId)}>
                  <span>{run.runId} · {run.status}</span>
                </button>
              )}
            </For>
          </div>
        </Show>
      </section>
    </div>
  )
}

function ActionSheet(props: { title: string; onClose: () => void; children: JSX.Element }) {
  return (
    <div class="sheet-backdrop" onClick={props.onClose}>
      <section class="action-sheet" onClick={(event) => event.stopPropagation()}>
        <div class="sheet-handle" />
        <div class="sheet-title">
          <h2>{props.title}</h2>
          <button class="icon-button" aria-label="close" onClick={props.onClose}><X size={18} /></button>
        </div>
        {props.children}
      </section>
    </div>
  )
}

function PreviewContent(props: { preview: FilePreview | null; error: string }) {
  return (
    <div class="preview-content">
      <Show when={props.error}><p class="error-text">{props.error}</p></Show>
      <Show when={props.preview}>
        {(preview) => (
          <>
            <p class="mono-line">{preview().path} · {preview().size} bytes {preview().truncated ? '· truncated' : ''}</p>
            <pre>{preview().text}</pre>
          </>
        )}
      </Show>
    </div>
  )
}

export function App() {
  const [snapshots, setSnapshots] = createSignal<SnapshotsPayload>(emptySnapshots())
  const [activeTab, setActiveTab] = createSignal<AppTab>('home')
  const [selectedStage, setSelectedStage] = createSignal<StageRoute>('routing')
  const [logs, setLogs] = createSignal<LogEntry[]>([])
  const [progress, setProgress] = createSignal<Record<string, string>>({})
  const [connection, setConnection] = createSignal('booting')
  const [selectedControlIndex, setSelectedControlIndex] = createSignal(0)
  const [resumeRuns, setResumeRuns] = createSignal<RunOption[]>([])
  const [logFilter, setLogFilter] = createSignal('all')
  const [preview, setPreview] = createSignal<FilePreview | null>(null)
  const [previewError, setPreviewError] = createSignal('')
  const [sheet, setSheet] = createSignal<SheetKind>(null)
  let scheduledRefreshTimer = 0
  let disconnectBridgeEvents: (() => void) | undefined

  const progressLine = createMemo(() => Object.values(progress()).filter(Boolean).join(' | '))

  const appendRuntimeLog = (event: BridgeEvent) => {
    setLogs((prev) => appendLog(prev, classifyLog(JSON.stringify(event.payload, null, 2), event.type, event.payload)))
  }

  const applySnapshots = (payload: SnapshotsPayload) => {
    setSnapshots(payload)
    setSelectedControlIndex((prev) => Math.min(prev, Math.max(payload.control.workers.length - 1, 0)))
    const activeStageRoute = stageRouteForAction(payload.app.activeStage)
    if (activeStageRoute) setSelectedStage(activeStageRoute)
  }

  const refreshSnapshots = async () => {
    const [nextSnapshots, prompt] = await Promise.all([getSnapshots(), getPrompt()])
    applySnapshots({ ...nextSnapshots, prompt })
    setConnection('online')
  }

  const queueRefreshSnapshots = (delayMs = 600) => {
    if (scheduledRefreshTimer) return
    scheduledRefreshTimer = window.setTimeout(() => {
      scheduledRefreshTimer = 0
      void refreshSnapshots().catch(() => setConnection('offline'))
    }, delayMs)
  }

  const handleEvent = (event: BridgeEvent) => {
    if (event.type === 'log.append') {
      setLogs((prev) => appendLog(prev, classifyLog(String(event.payload.text ?? ''), event.type, event.payload)))
      return
    }
    if (event.type === 'progress.start' || event.type === 'progress.update') {
      const id = String(event.payload.id ?? '')
      const line = String(event.payload.line ?? '')
      setProgress((prev) => ({ ...prev, [id]: line }))
      queueRefreshSnapshots()
      return
    }
    if (event.type === 'progress.stop') {
      const id = String(event.payload.id ?? '')
      setProgress((prev) => {
        const next = { ...prev }
        delete next[id]
        return next
      })
      queueRefreshSnapshots(200)
      return
    }
    if (event.type === 'prompt.request') {
      setSnapshots((prev) => ({
        ...prev,
        prompt: normalizePromptSnapshot({
          pending: true,
          prompt_id: event.payload.id,
          prompt_type: event.payload.prompt_type,
          payload: event.payload,
        }),
      }))
      setActiveTab('home')
      queueRefreshSnapshots(150)
      return
    }
    if (event.type === 'snapshot.app') {
      setSnapshots((prev) => ({ ...prev, app: normalizeAppSnapshot(event.payload) }))
      return
    }
    if (event.type === 'snapshot.stage') {
      const stageRoute = String(event.payload.route ?? '') as StageRoute
      if (!STAGE_ROUTES.includes(stageRoute)) return
      setSnapshots((prev) => ({
        ...prev,
        stages: { ...prev.stages, [stageRoute]: normalizeStageSnapshot(event.payload.snapshot) },
      }))
      return
    }
    if (event.type === 'snapshot.control') {
      setSnapshots((prev) => ({ ...prev, control: normalizeControlSnapshot(event.payload) }))
      return
    }
    if (event.type === 'snapshot.hitl') {
      setSnapshots((prev) => ({ ...prev, hitl: normalizeHitlSnapshot(event.payload) }))
      return
    }
    if (event.type === 'snapshot.artifacts') {
      setSnapshots((prev) => ({ ...prev, artifacts: normalizeArtifactsSnapshot(event.payload) as ArtifactsSnapshot }))
      return
    }
    if (event.type === 'stage.changed') {
      setLogs((prev) => appendLog(prev, classifyLog(`stage ${String(event.payload.action ?? '')}: ${String(event.payload.status ?? '')}`, event.type, event.payload)))
      queueRefreshSnapshots(150)
      return
    }
    appendRuntimeLog(event)
  }

  const openPreview = async (path: string) => {
    setPreview(null)
    setPreviewError('')
    setSheet('preview')
    try {
      setPreview(await getFilePreview(path))
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : String(error))
    }
  }

  const submitPrompt = async (value: unknown) => {
    const prompt = snapshots().prompt
    if (!prompt.pending || !prompt.promptId) return
    await submitPromptResponse(prompt.promptId, value)
    setSnapshots((prev) => ({ ...prev, prompt: EMPTY_PROMPT }))
    await refreshSnapshots()
  }

  const startWorkflow = async () => {
    await postBridgeRequest('workflow.a00.start', { argv: [] })
    setLogs((prev) => appendLog(prev, classifyLog('已请求从路由层开始')))
    await refreshSnapshots()
  }

  const performControlAction = async (action: 'attach' | 'detach' | 'restart' | 'retry' | 'kill') => {
    const control = snapshots().control
    const worker = control.workers[selectedControlIndex()]
    if (!control.controlId || !worker) return
    const argument = String(worker.index || selectedControlIndex() + 1)
    const mapping = {
      attach: 'worker.attach',
      detach: 'worker.detach',
      restart: 'worker.restart',
      retry: 'worker.retry',
      kill: 'worker.kill',
    } as const
    const result = await postBridgeRequest(mapping[action], { control_id: control.controlId, argument })
    if (action === 'attach') {
      const command = Array.isArray(result.attach_command) ? result.attach_command.map(String).join(' ') : `tmux attach -t ${worker.sessionName}`
      await copyText(command)
      setLogs((prev) => appendLog(prev, classifyLog(`attach command copied: ${command}`)))
    }
    await refreshSnapshots()
  }

  const openResumeRuns = async () => {
    const result = await postBridgeRequest('run.list', { project_dir: snapshots().app.projectDir })
    const rawRuns = Array.isArray(result.runs) ? result.runs as Record<string, unknown>[] : []
    setResumeRuns(rawRuns.map((run) => ({
      runId: String(run.run_id ?? ''),
      runtimeDir: String(run.runtime_dir ?? ''),
      projectDir: String(run.project_dir ?? ''),
      status: String(run.status ?? ''),
      updatedAt: String(run.updated_at ?? ''),
      workerCount: Number(run.worker_count ?? 0),
      failedCount: Number(run.failed_count ?? 0),
    })))
  }

  const resumeRun = async (runId: string) => {
    if (!runId) return
    await postBridgeRequest('run.resume', {
      control_id: snapshots().control.controlId,
      run_id: runId,
      project_dir: snapshots().app.projectDir,
    })
    setResumeRuns([])
    await refreshSnapshots()
  }

  onMount(() => {
    disconnectBridgeEvents = connectBridgeEvents(handleEvent, {
      onError: () => {
        setConnection('reconnecting')
        void refreshSnapshots().catch(() => setConnection('offline'))
      },
    })
    void (async () => {
      try {
        const bootstrap = await getBootstrap()
        applySnapshots(bootstrap.snapshots)
        const prompt = await getPrompt()
        setSnapshots((prev) => ({ ...prev, prompt }))
        setConnection('online')
      } catch (error) {
        setConnection('offline')
        setLogs((prev) => appendLog(prev, classifyLog(error instanceof Error ? error.message : String(error), 'error')))
      }
    })()
  })

  onCleanup(() => {
    if (scheduledRefreshTimer) window.clearTimeout(scheduledRefreshTimer)
    disconnectBridgeEvents?.()
  })

  return (
    <div class="app-shell">
      <AppHeader app={snapshots().app} connection={connection()} progress={progressLine()} onRefresh={() => void refreshSnapshots()} onMore={() => setSheet('advanced')} />
      <main class="app-main">
        <Switch>
          <Match when={activeTab() === 'home'}>
            <HomeView
              snapshots={snapshots()}
              progress={progressLine()}
              onPromptSubmit={submitPrompt}
              onStartWorkflow={startWorkflow}
              onPreview={(path) => void openPreview(path)}
            />
          </Match>
          <Match when={activeTab() === 'stages'}>
            <StagesView snapshots={snapshots()} selectedStage={selectedStage()} onSelectStage={setSelectedStage} onPreview={(path) => void openPreview(path)} />
          </Match>
          <Match when={activeTab() === 'files'}>
            <FilesView snapshots={snapshots()} selectedStage={selectedStage()} onPreview={(path) => void openPreview(path)} />
          </Match>
          <Match when={activeTab() === 'logs'}>
            <LogsView logs={logs()} filter={logFilter()} onFilter={setLogFilter} />
          </Match>
        </Switch>
      </main>
      <BottomTabs activeTab={activeTab()} onSelect={setActiveTab} />
      <Show when={sheet() === 'advanced'}>
        <ActionSheet title="更多" onClose={() => setSheet(null)}>
          <AdvancedPanel
            snapshots={snapshots()}
            selectedControlIndex={selectedControlIndex()}
            onControlSelect={setSelectedControlIndex}
            onControlAction={(action) => void performControlAction(action)}
            onRefresh={() => void refreshSnapshots()}
            resumeRuns={resumeRuns()}
            onOpenResume={() => void openResumeRuns()}
            onResumeRun={(runId) => void resumeRun(runId)}
          />
        </ActionSheet>
      </Show>
      <Show when={sheet() === 'preview'}>
        <ActionSheet title={preview()?.path.split('/').at(-1) || '预览'} onClose={() => {
          setSheet(null)
          setPreview(null)
          setPreviewError('')
        }}>
          <PreviewContent preview={preview()} error={previewError()} />
        </ActionSheet>
      </Show>
    </div>
  )
}
