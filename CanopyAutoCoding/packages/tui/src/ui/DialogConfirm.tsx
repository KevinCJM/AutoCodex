import { DialogSelect } from './DialogSelect'

type Props = {
  title: string
  defaultValue?: boolean
  active?: boolean
  onSubmit: (value: boolean) => void
}

export function DialogConfirm(props: Props) {
  return (
    <DialogSelect
      title={props.title}
      defaultValue={props.defaultValue === false ? 'no' : 'yes'}
      active={props.active}
      options={[
        { value: 'yes', label: 'yes' },
        { value: 'no', label: 'no' },
      ]}
      onSubmit={(value) => props.onSubmit(value === 'yes')}
    />
  )
}
