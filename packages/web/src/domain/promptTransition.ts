import type { PromptSnapshot } from './types'

export type PromptResponseTransition = {
  clearPrompt: boolean
  accepted: boolean
}

export function resolvePromptResponseTransition(
  submittedPromptId: string,
  currentPrompt: PromptSnapshot,
  accepted: boolean,
): PromptResponseTransition {
  if (!accepted) return { clearPrompt: false, accepted: false }
  if (currentPrompt.pending && currentPrompt.promptId && currentPrompt.promptId !== submittedPromptId) {
    return { clearPrompt: false, accepted: true }
  }
  return { clearPrompt: true, accepted: true }
}

export function resolvePromptAwareStatus(status: string, hasActivePrompt: boolean): string {
  const normalized = String(status ?? '').trim().toLowerCase() || 'running'
  if (!hasActivePrompt) return normalized
  if (normalized === 'failed' || normalized === 'error' || normalized === 'completed') return normalized
  return 'awaiting-input'
}
