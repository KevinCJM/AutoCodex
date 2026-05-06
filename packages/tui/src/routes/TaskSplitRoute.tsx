import { For, Show } from 'solid-js'
import type { TaskSplitSnapshot } from '../types'

type Props = {
  snapshot: TaskSplitSnapshot
}

export function TaskSplitRoute(props: Props) {
  return (
    <box flexDirection="column" gap={1} paddingLeft={1} paddingRight={1} flexGrow={1}>
      <text>任务拆分</text>
      <text fg="#888888">{`项目目录: ${props.snapshot.projectDir || '(unset)'}`}</text>
      <text fg="#888888">{`需求名称: ${props.snapshot.requirementName || '(unset)'}`}</text>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
        <text>阶段文档</text>
        <Show when={props.snapshot.files.length > 0} fallback={<text fg="#888888">尚未发现任务拆分产物。</text>}>
          <For each={props.snapshot.files}>
            {(item) => (
              <text fg={item.exists ? '#00d2ff' : '#888888'}>{`${item.label}: ${item.exists ? 'ready' : 'missing'}`}</text>
            )}
          </For>
        </Show>
      </box>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
        <text>Workers</text>
        <Show when={props.snapshot.workers.length > 0} fallback={<text fg="#888888">当前没有任务拆分 workers。</text>}>
          <For each={props.snapshot.workers}>
            {(worker) => <text>{`${worker.sessionName} | ${worker.workflowStage}/${worker.agentState} | ${worker.healthStatus}`}</text>}
          </For>
        </Show>
      </box>
      <Show when={props.snapshot.blockers.length > 0}>
        <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
          <text>阻塞项</text>
          <For each={props.snapshot.blockers}>
            {(item) => <text fg="#f7c948">{item}</text>}
          </For>
        </box>
      </Show>
    </box>
  )
}
