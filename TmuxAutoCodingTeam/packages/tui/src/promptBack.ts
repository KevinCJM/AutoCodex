import type { SelectOption } from './ui/DialogSelect'

export const PROMPT_BACK_VALUE = '__tmux_back__'
export const PROMPT_BACK_LABEL = '[上一步]'

export function promptAllowsBack(payload: Record<string, unknown> | null | undefined): boolean {
  const value = payload?.allow_back ?? payload?.allowBack
  return value === true || value === 'true' || value === 1 || value === '1'
}

export function resolvePromptBackValue(payload: Record<string, unknown> | null | undefined): string {
  const value = payload?.back_value ?? payload?.backValue
  const text = String(value ?? '').trim()
  return text || PROMPT_BACK_VALUE
}

export function withPromptBackOption(
  options: ReadonlyArray<SelectOption>,
  payload: Record<string, unknown> | null | undefined,
): SelectOption[] {
  const normalized = options.map((item) => ({
    value: String(item.value),
    label: String(item.label),
  }))
  if (!promptAllowsBack(payload)) return normalized

  const backValue = resolvePromptBackValue(payload)
  if (normalized.some((item) => item.value === backValue)) return normalized
  return [...normalized, { value: backValue, label: PROMPT_BACK_LABEL }]
}
