import { For, Show, createMemo } from 'solid-js'
import { PromptTextarea } from './PromptTextarea'

type Props = {
  title: string
  defaultValue?: string
  draftKey?: string
  focusToken?: string
  focused?: boolean
  mode?: 'singleline' | 'multiline'
  hintLines?: string[]
  showSubmitHelper?: boolean
  onSubmit: (value: string) => void
}

export function PromptInputPanel(props: Props) {
  const multiline = createMemo(() => props.mode !== 'singleline')
  const helperText = createMemo(() =>
    props.showSubmitHelper && multiline() ? 'Enter 提交，Shift+Enter / Meta+Enter / Ctrl+J 换行' : ''
  )

  return (
    <box flexDirection="column" paddingLeft={1} paddingRight={1} paddingTop={1} width="100%">
      <text>{props.title}</text>
      <Show when={helperText()}>
        <text fg="#888888">{helperText()}</text>
      </Show>
      <For each={props.hintLines ?? []}>{(line) => <text fg="#888888">{line}</text>}</For>
      <PromptTextarea
        initialValue={props.defaultValue ?? ''}
        draftKey={props.draftKey}
        focusToken={props.focusToken}
        focused={props.focused}
        multiline={multiline()}
        height={multiline() ? 5 : 1}
        minHeight={multiline() ? 5 : 1}
        maxHeight={multiline() ? 5 : 1}
        placeholder=""
        onSubmit={props.onSubmit}
      />
    </box>
  )
}
