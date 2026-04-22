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
): PromptResponseTransition {
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
