import { createMemo } from 'solid-js'

import { resolvePromptBackValue, withPromptBackOption } from '../promptBack'
import { DialogSelect } from './DialogSelect'

type Props = {
  title: string
  defaultValue?: boolean
  active?: boolean
  allowBack?: boolean
  backValue?: string
  onSubmit: (value: boolean | string) => void
}

export function DialogConfirm(props: Props) {
  const backPayload = createMemo(() => ({
    allow_back: Boolean(props.allowBack),
    back_value: props.backValue,
  }))
  const backValue = createMemo(() => resolvePromptBackValue(backPayload()))
  const options = createMemo(() => withPromptBackOption([
    { value: 'yes', label: 'yes' },
    { value: 'no', label: 'no' },
  ], backPayload()))

  return (
    <DialogSelect
      title={props.title}
      defaultValue={props.defaultValue === false ? 'no' : 'yes'}
      active={props.active}
      options={options()}
      onSubmit={(value) => props.onSubmit(value === backValue() ? backValue() : value === 'yes')}
    />
  )
}
