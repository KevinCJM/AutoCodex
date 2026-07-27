import { For, Show, createMemo, createSignal, onCleanup } from 'solid-js'
import { graphifyStatusColor, graphifyStatusLabel } from '../graphifyStatus'
import type { AppSnapshot, GraphifyStatus, HitlSnapshot, HomeAgentItem, StageFailureSnapshot } from '../types'

type Props = {
  snapshot: AppSnapshot
  hitl: HitlSnapshot
  agents: HomeAgentItem[]
}

const HOME_AGENT_SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

function AgentStatusMarker(props: { agentState: string }) {
  const [frameIndex, setFrameIndex] = createSignal(0)
  const normalizedState = createMemo(() => String(props.agentState || '').trim().toUpperCase())
  const isBusy = createMemo(() => normalizedState() === 'BUSY')
  const timer = setInterval(() => {
    if (!isBusy()) return
    setFrameIndex((prev) => (prev + 1) % HOME_AGENT_SPINNER_FRAMES.length)
  }, 400)
  onCleanup(() => clearInterval(timer))

  const marker = createMemo(() => (isBusy() ? HOME_AGENT_SPINNER_FRAMES[frameIndex()] : '•'))
  const color = createMemo(() => {
    if (normalizedState() === 'BUSY') return '#f7c948'
    if (normalizedState() === 'DEAD') return '#ff5d5d'
    if (normalizedState() === 'READY') return '#00d2ff'
    if (normalizedState() === 'STARTING') return '#888888'
    return '#888888'
  })

  const markerText = createMemo(() => {
    if (normalizedState() === 'DEAD') return '❌'
    return marker()
  })

  return <text fg={color()}>{markerText()}</text>
}

function agentSummary(agent: HomeAgentItem): string {
  const stateText = `${agent.healthStatus}/${agent.agentState}`
  return [agent.sessionName, agent.agentConfigLabel, stateText].filter(Boolean).join(' | ')
}

export function HomeRoute(props: Props) {
  return (
    <box flexDirection="column" gap={1} paddingLeft={1} paddingRight={1} flexGrow={1}>
      <text>总览</text>
      <text fg="#888888">当前阶段: {props.snapshot.activeStageLabel || '等待中'}</text>
      <text fg="#888888">{`项目目录: ${props.snapshot.projectDir || '(unset)'}`}</text>
      <Show when={props.snapshot.requirementName}>
        <text fg="#888888">{`需求名称: ${props.snapshot.requirementName}`}</text>
      </Show>
      <Show when={props.snapshot.graphify} keyed>
        {(graphify: GraphifyStatus) => (
          <box flexDirection="column">
            <text fg={graphifyStatusColor(graphify)}>
              {`代码图谱: ${graphifyStatusLabel(graphify)}${graphify.version ? ` · v${graphify.version}` : ''}`}
            </text>
            <Show when={graphify.nodeCount > 0 || graphify.edgeCount > 0}>
              <text fg="#888888">{`${graphify.nodeCount} nodes / ${graphify.edgeCount} edges`}</text>
            </Show>
            <Show when={graphify.lastError}>
              <text fg="#888888">{graphify.lastError}</text>
            </Show>
          </box>
        )}
      </Show>
      <Show when={props.snapshot.activeStageFailure} keyed>
        {(failure: StageFailureSnapshot) => (
          <box borderStyle="single" borderColor="#ff5d5d" paddingLeft={1} paddingRight={1} paddingTop={1} paddingBottom={1} flexDirection="column">
            <text fg="#ff5d5d">阶段执行失败</text>
            <text fg="#ffb3b3">{failure.message || failure.failureKind || 'unknown error'}</text>
            <Show when={failure.failurePath}>
              <text fg="#888888">failure: {failure.failurePath}</text>
            </Show>
            <For each={failure.orphanedWorkers}>
              {(worker) => <text fg="#f7c948">{worker.attachCommand}</text>}
            </For>
          </box>
        )}
      </Show>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} paddingTop={1} paddingBottom={1} flexDirection="column">
        <text>待处理人工输入</text>
        <text fg={props.hitl.pending || props.snapshot.pendingAttention ? '#f7c948' : '#888888'}>
          {props.hitl.pending ? '存在待处理 HITL' : props.snapshot.pendingAttention ? '存在待处理人工输入' : '当前没有待处理人工输入'}
        </text>
        <Show when={props.hitl.summary}>
          <text fg="#f7c948">{props.hitl.summary}</text>
        </Show>
        <Show when={props.hitl.reasonText}>
          <text fg="#888888">reason: {props.hitl.reasonText}</text>
        </Show>
        <Show when={props.hitl.questionPath}>
          <text fg="#888888">question: {props.hitl.questionPath}</text>
        </Show>
        <For each={props.hitl.targetPaths ?? []}>
          {(path) => <text fg="#888888">target: {path}</text>}
        </For>
        <Show when={props.hitl.attachCommand}>
          <text fg="#f7c948">{props.hitl.attachCommand}</text>
        </Show>
        <Show when={props.snapshot.pendingAttention}>
          <text fg="#f7c948">{`attention: ${props.snapshot.pendingAttentionReason || '待处理人工输入'}`}</text>
          <Show when={props.snapshot.pendingAttentionSince}>
            <text fg="#888888">{`since: ${props.snapshot.pendingAttentionSince}`}</text>
          </Show>
        </Show>
        <Show when={props.hitl.pending}>
          <text fg="#888888">Ctrl+L 查看完整日志</text>
        </Show>
      </box>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} paddingTop={1} paddingBottom={1} flexDirection="column">
        <text>智能体状态</text>
        <Show when={props.agents.length > 0} fallback={<text fg="#888888">当前没有可显示的智能体状态。</text>}>
          <For each={props.agents}>
            {(agent) => (
              <box flexDirection="column" marginTop={1}>
                <box flexDirection="row" gap={1}>
                  <AgentStatusMarker agentState={agent.agentState} />
                  <text>{agentSummary(agent)}</text>
                </box>
                <text fg="#888888">{agent.attachCommand}</text>
              </box>
            )}
          </For>
        </Show>
      </box>
    </box>
  )
}
