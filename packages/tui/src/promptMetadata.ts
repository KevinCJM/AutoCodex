export type PromptMetadata = {
  interactionKind: string
  questionIndex: number | null
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

function questionIndex(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

export function resolvePromptMetadata(payload: Record<string, unknown>): PromptMetadata {
  const interactionKind = text(payload.interaction_kind ?? payload.interactionKind)
  const recoveryKind = text(payload.recovery_kind ?? payload.recoveryKind)
  const normalizedInteraction = interactionKind.toLowerCase().replaceAll('_', '-')
  const normalizedRecovery = recoveryKind.toLowerCase().replaceAll('_', '-')
  return {
    interactionKind,
    questionIndex: questionIndex(payload.question_index ?? payload.questionIndex),
    recommendation: text(
      payload.recommendation
      ?? payload.recommended_answer
      ?? payload.recommendedAnswer,
    ),
    reasonText: text(payload.reason_text ?? payload.reasonText),
    isGrill: normalizedInteraction.startsWith('grill') || normalizedRecovery === 'grill-decision',
  }
}

function limitedLines(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).slice(0, 3)
}

export function buildPromptMetadataHintLines(payload: Record<string, unknown>): string[] {
  const metadata = resolvePromptMetadata(payload)
  const lines: string[] = []
  if (metadata.isGrill && metadata.questionIndex !== null) {
    lines.push(`Grill 第 ${metadata.questionIndex} 轮`)
  }
  const reasonLines = limitedLines(metadata.reasonText)
  lines.push(...reasonLines.map((line, index) => (
    index === 0 ? `${metadata.isGrill ? '为什么需要决定' : '原因'}: ${line}` : line
  )))
  const recommendationLines = limitedLines(metadata.recommendation)
  lines.push(...recommendationLines.map((line, index) => (
    index === 0 ? `推荐答案: ${line}` : line
  )))
  const answerOptions = promptAnswerOptions(payload)
  if (answerOptions.length > 0) lines.push(`可选答案: ${answerOptions.join(' / ')}`)
  return lines
}
