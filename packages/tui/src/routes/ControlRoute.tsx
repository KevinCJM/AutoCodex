import { For, Show, createMemo } from 'solid-js'
import type { ControlSnapshot } from '../types'

type Props = {
  snapshot: ControlSnapshot | null
  selectedWorkerIndex: number
}

export function ControlRoute(props: Props) {
  const workers = createMemo(() => props.snapshot?.workers ?? [])
  const selectedWorker = createMemo(() => workers()[props.selectedWorkerIndex] ?? workers()[0] ?? null)

  return (
    <box flexDirection="column" gap={1} paddingLeft={1} paddingRight={1} flexGrow={1}>
      <text>控制台</text>
      <Show when={props.snapshot} keyed fallback={<text fg="#888888">当前没有激活的 control 会话。</text>}>
        {(snapshot: ControlSnapshot) => (
          <>
            <text fg="#888888">{`run_id: ${snapshot.runId || '(none)'}`}</text>
            <text fg="#888888">{`runtime_dir: ${snapshot.runtimeDir || '(none)'}`}</text>
            <text fg="#888888">{snapshot.done ? '当前 run 已完成' : '当前 run 仍在执行中'}</text>
            <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
              <text>Workers</text>
              <Show when={snapshot.workers.length > 0} fallback={<text fg="#888888">当前没有 worker。</text>}>
                <For each={snapshot.workers}>
                  {(worker, index) => (
                    <text fg={index() === props.selectedWorkerIndex ? '#00d2ff' : '#ffffff'}>{`${index() + 1}. ${worker.status} | ${worker.sessionName} | ${worker.workflowStage}/${worker.agentState} | ${worker.healthStatus}`}</text>
                  )}
                </For>
              </Show>
            </box>
            <Show when={selectedWorker()}>
              {(worker: () => NonNullable<ReturnType<typeof selectedWorker>>) => (
                <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
                  <text>当前选中</text>
                  <text>{`session: ${worker().sessionName || '(none)'}`}</text>
                  <text>{`work_dir: ${worker().workDir || '(none)'}`}</text>
                  <text>{`turn_status: ${worker().turnStatusPath || '(none)'}`}</text>
                  <Show when={worker().questionPath}>
                    <text>{`question: ${worker().questionPath}`}</text>
                  </Show>
                  <Show when={worker().answerPath}>
                    <text>{`answer: ${worker().answerPath}`}</text>
                  </Show>
                  <Show when={worker().artifactPaths.length > 0}>
                    <text>{`artifacts: ${worker().artifactPaths.length}`}</text>
                  </Show>
                  <text fg="#888888">{worker().note || 'no note'}</text>
                </box>
              )}
            </Show>
            <Show when={snapshot.finalSummary}>
              <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
                <text>Summary</text>
                <text>{snapshot.finalSummary}</text>
              </box>
            </Show>
            <Show when={snapshot.transitionText}>
              <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
                <text>Transition</text>
                <text>{snapshot.transitionText}</text>
              </box>
            </Show>
          </>
        )}
      </Show>
      <box borderStyle="single" paddingLeft={1} paddingRight={1} flexDirection="column">
        <text>快捷键</text>
        <text fg="#888888">↑/↓ 或 j/k 选 worker | Enter attach | r restart | t retry | k kill | d detach | u resume | g 返回总览</text>
      </box>
    </box>
  )
}
