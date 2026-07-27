export type PromptMetadata = {
  interactionKind: string
  questionIndex?: number
  recommendation: string
  reasonText: string
  isGrill: boolean
}

function text(value: unknown): string {
  return String(value ?? '').trim()
}

export function promptCanSubmit(payload: Record<string, unknown>): boolean {
  return payload.can_submit !== false && payload.canSubmit !== false
}

export function promptRecoveryMessage(payload: Record<string, unknown>): string {
  return text(payload.recovery_message ?? payload.recoveryMessage)
}

export function promptAnswerOptions(payload: Record<string, unknown>): string[] {
  const raw = payload.answer_options ?? payload.answerOptions
  return Array.isArray(raw) ? raw.map(text).filter(Boolean).slice(0, 4) : []
}

function optionalQuestionIndex(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '') return undefined
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : undefined
}

export function resolvePromptMetadata(
  container: Record<string, unknown>,
  payload: Record<string, unknown> = {},
): PromptMetadata {
  const interactionKind = text(
    container.interaction_kind
    ?? container.interactionKind
    ?? payload.interaction_kind
    ?? payload.interactionKind,
  )
  const recoveryKind = text(
    container.recovery_kind
    ?? container.recoveryKind
    ?? payload.recovery_kind
    ?? payload.recoveryKind,
  )
  const normalizedInteraction = interactionKind.toLowerCase().replaceAll('_', '-')
  const normalizedRecovery = recoveryKind.toLowerCase().replaceAll('_', '-')
  return {
    interactionKind,
    questionIndex: optionalQuestionIndex(
      container.question_index
      ?? container.questionIndex
      ?? payload.question_index
      ?? payload.questionIndex,
    ),
    recommendation: text(
      container.recommendation
      ?? container.recommended_answer
      ?? container.recommendedAnswer
      ?? payload.recommendation
      ?? payload.recommended_answer
      ?? payload.recommendedAnswer,
    ),
    reasonText: text(
      container.reason_text
      ?? container.reasonText
      ?? payload.reason_text
      ?? payload.reasonText,
    ),
    isGrill: normalizedInteraction.startsWith('grill') || normalizedRecovery === 'grill-decision',
  }
}

export function grillPromptKicker(metadata: PromptMetadata): string {
  if (!metadata.isGrill) return '需要你输入'
  return metadata.questionIndex ? `Grill 第 ${metadata.questionIndex} 轮` : 'Grill 需要你输入'
}
