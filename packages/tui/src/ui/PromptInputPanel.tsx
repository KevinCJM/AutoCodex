import { useKeyboard } from '@opentui/solid'
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
  onBack?: () => void
  onSubmit: (value: string) => void
}

export function PromptInputPanel(props: Props) {
  const multiline = createMemo(() => props.mode !== 'singleline')
  const helperText = createMemo(() =>
    props.showSubmitHelper && multiline() ? 'Enter 提交，Shift+Enter / Meta+Enter / Ctrl+J 换行' : ''
  )

  useKeyboard((event) => {
    if (!props.focused || !props.onBack) return
    if (event.name !== 'escape') return
    event.preventDefault()
    props.onBack()
  })

  return (
    <box flexDirection="column" paddingLeft={1} paddingRight={1} paddingTop={1} width="100%">
      <text>{props.title}</text>
      <Show when={props.onBack}>
        <box
          flexDirection="row"
          gap={1}
          onMouseUp={(event) => {
            event.preventDefault()
            event.stopPropagation()
            props.onBack?.()
          }}
        >
          <text fg="#00d2ff">[上一步]</text>
          <text fg="#888888">Esc</text>
        </box>
      </Show>
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
