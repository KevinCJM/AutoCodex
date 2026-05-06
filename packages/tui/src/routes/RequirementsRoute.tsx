import { For, Show } from 'solid-js'
import type { RequirementsSnapshot } from '../types'

type Props = {
  snapshot: RequirementsSnapshot
}

export function RequirementsRoute(props: Props) {
  return (
    <box flexDirection="column" gap={1} paddingLeft={1} paddingRight={1} flexGrow={1}>
      <text>需求录入 / 需求澄清</text>
      <text fg="#888888">{`项目目录: ${props.snapshot.projectDir || '(unset)'}`}</text>
      <text fg="#888888">{`需求名称: ${props.snapshot.requirementName || '(unset)'}`}</text>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
        <text>阶段文档</text>
        <Show when={props.snapshot.files.length > 0} fallback={<text fg="#888888">尚未发现需求录入/澄清产物。</text>}>
          <For each={props.snapshot.files}>
            {(item) => (
              <text fg={item.exists ? '#00d2ff' : '#888888'}>{`${item.label}: ${item.exists ? 'ready' : 'missing'}`}</text>
            )}
          </For>
        </Show>
      </box>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
        <text>Workers</text>
        <Show when={props.snapshot.workers.length > 0} fallback={<text fg="#888888">当前没有需求录入/澄清 workers。</text>}>
          <For each={props.snapshot.workers}>
            {(worker) => <text>{`${worker.sessionName} | ${worker.workflowStage}/${worker.agentState} | ${worker.healthStatus}`}</text>}
          </For>
        </Show>
      </box>
    </box>
  )
}
