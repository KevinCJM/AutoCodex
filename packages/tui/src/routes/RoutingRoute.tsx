import { For, Show } from 'solid-js'
import type { RoutingSnapshot } from '../types'

type Props = {
  snapshot: RoutingSnapshot
}

export function RoutingRoute(props: Props) {
  return (
    <box flexDirection="column" gap={1} paddingLeft={1} paddingRight={1} flexGrow={1}>
      <text>路由层</text>
      <text fg="#888888">{`项目目录: ${props.snapshot.projectDir || '(unset)'}`}</text>
      <text fg="#888888">{props.snapshot.done ? 'routing 已完成' : props.snapshot.statusText || '等待 routing run'}</text>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
        <text>目标文件</text>
        <Show when={props.snapshot.files.length > 0} fallback={<text fg="#888888">尚未发现路由层目标文件。</text>}>
          <For each={props.snapshot.files}>
            {(item) => <text fg={item.exists ? '#00d2ff' : '#ff6b6b'}>{`${item.label}: ${item.exists ? 'ready' : 'missing'}`}</text>}
          </For>
        </Show>
      </box>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
        <text>Workers</text>
        <Show when={props.snapshot.workers.length > 0} fallback={<text fg="#888888">当前没有 routing workers。</text>}>
          <For each={props.snapshot.workers}>
            {(worker) => <text>{`${worker.sessionName} | ${worker.workflowStage}/${worker.agentState} | ${worker.healthStatus}`}</text>}
          </For>
        </Show>
      </box>
    </box>
  )
}
