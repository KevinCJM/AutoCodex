export type PromptTransitionFocus = 'content' | 'prompt' | 'dialog'
export type PromptTransitionStatus = 'running' | 'awaiting-input'

export type PromptTransitionActivePrompt = {
  id: string
  promptType: string
}

export type PromptResponseTransition = {
  clearPrompt: boolean
  nextStatus: PromptTransitionStatus
  nextShellFocus: PromptTransitionFocus
}

function isOverlayPromptType(promptType: string): boolean {
  return promptType === 'select' || promptType === 'confirm'
}

export function resolvePromptResponseTransition(
  submittedPromptId: string,
  currentPrompt: PromptTransitionActivePrompt | null,
  accepted: boolean,
): PromptResponseTransition {
  if (!accepted) {
    return {
      clearPrompt: false,
      nextStatus: 'awaiting-input',
      nextShellFocus: currentPrompt && isOverlayPromptType(currentPrompt.promptType) ? 'dialog' : 'prompt',
    }
  }
  if (currentPrompt && currentPrompt.id !== submittedPromptId) {
    return {
      clearPrompt: false,
      nextStatus: 'awaiting-input',
      nextShellFocus: isOverlayPromptType(currentPrompt.promptType) ? 'dialog' : 'prompt',
    }
  }
  return {
    clearPrompt: true,
    nextStatus: 'running',
    nextShellFocus: 'content',
  }
}

export function resolvePromptAwareStatus(status: string, hasActivePrompt: boolean): string {
  const normalized = String(status ?? '').trim().toLowerCase() || 'running'
  if (!hasActivePrompt) return normalized
  if (normalized === 'failed' || normalized === 'error' || normalized === 'completed') return normalized
  return 'awaiting-input'
}
