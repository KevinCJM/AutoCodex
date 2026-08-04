export type BootstrapPromptState = {
  id: string
  promptType: string
  payload: Record<string, unknown>
  draftKey: string
}

const PROMPT_METADATA_KEYS = [
  ['interaction_kind', 'interactionKind'],
  ['question_index', 'questionIndex'],
  ['owner_runner_id', 'ownerRunnerId'],
  ['question_seq', 'questionSeq'],
  ['ready_for_human', 'readyForHuman'],
  ['owner_session_name', 'ownerSessionName'],
  ['owner_turn_id', 'ownerTurnId'],
  ['owner_state_revision', 'ownerStateRevision'],
  ['recommendation', 'recommendation'],
  ['reason_text', 'reasonText'],
] as const

const AUTHORITATIVE_PROMPT_CURSOR_KEYS = new Set(['owner_runner_id', 'question_seq'])

function promptPayloadWithMetadata(
  container: Record<string, unknown>,
  payload: Record<string, unknown>,
): Record<string, unknown> {
  const normalized = { ...payload }
  for (const [snakeKey, camelKey] of PROMPT_METADATA_KEYS) {
    const value = container[snakeKey] ?? container[camelKey]
    if (AUTHORITATIVE_PROMPT_CURSOR_KEYS.has(snakeKey) && value !== undefined) {
      delete normalized[snakeKey]
      delete normalized[camelKey]
      normalized[snakeKey] = value
      continue
    }
    if (normalized[snakeKey] !== undefined || normalized[camelKey] !== undefined) continue
    if (value !== undefined) normalized[snakeKey] = value
  }
  return normalized
}

export type PromptSyncSource = 'bootstrap' | 'live'

export type PromptSyncUpdate = {
  prompt: BootstrapPromptState | null
  revision: number | null
  source: PromptSyncSource
}

export type PromptSyncState = {
  prompt: BootstrapPromptState | null
  revision: number | null
  liveUpdateSeen: boolean
  signature: string
}

export type PromptSyncTransition = {
  accepted: boolean
  state: PromptSyncState
}

export const EMPTY_PROMPT_SYNC_STATE: PromptSyncState = {
  prompt: null,
  revision: null,
  liveUpdateSeen: false,
  signature: 'pending:false',
}

type PromptOwnerWorker = {
  sessionName?: string
  stateRevision?: number
  agentState?: string
  turnState?: string
  currentTaskRuntimeStatus?: string
  currentTurnId?: string
}

function optionalBoolean(value: unknown): boolean | null {
  if (value === undefined || value === null || value === '') return null
  if (typeof value === 'boolean') return value
  const normalized = String(value).trim().toLowerCase()
  if (normalized === 'true' || normalized === '1') return true
  if (normalized === 'false' || normalized === '0') return false
  return null
}

export function workerOwnedPromptIsReady(
  prompt: BootstrapPromptState | null,
  workers: PromptOwnerWorker[],
): boolean {
  if (!prompt) return true
  const payload = prompt.payload
  const explicitlyReady = optionalBoolean(payload.ready_for_human ?? payload.readyForHuman)
  if (explicitlyReady === null) return true
  if (!explicitlyReady) return false
  const ownerSession = String(payload.owner_session_name ?? payload.ownerSessionName ?? '').trim()
  if (!ownerSession) return true
  const owner = workers.find((worker) => String(worker.sessionName ?? '').trim() === ownerSession)
  if (!owner) return false
  const ownerRevision = Number(payload.owner_state_revision ?? payload.ownerStateRevision ?? 0)
  if (Number.isSafeInteger(ownerRevision) && ownerRevision > 0 && Number(owner.stateRevision ?? 0) < ownerRevision) {
    return false
  }
  if (String(owner.agentState ?? '').trim().toUpperCase() !== 'READY') return false
  const ownerTurnId = String(payload.owner_turn_id ?? payload.ownerTurnId ?? '').trim()
  const workerTurnId = String(owner.currentTurnId ?? '').trim()
  if (ownerTurnId && workerTurnId && ownerTurnId !== workerTurnId) return false
  const active = new Set(['preparing', 'submitting', 'submitted', 'waiting_result', 'submission_unknown', 'running'])
  if (active.has(String(owner.turnState ?? '').trim().toLowerCase())) return false
  if (active.has(String(owner.currentTaskRuntimeStatus ?? '').trim().toLowerCase())) return false
  return true
}

function promptSignature(prompt: BootstrapPromptState | null): string {
  if (!prompt) return 'pending:false'
  return `pending:true:${prompt.id}:${prompt.promptType}`
}

export function promptRevisionFromPayload(payload: unknown): number | null {
  if (!payload || typeof payload !== 'object') return null
  const value = payload as Record<string, unknown>
  const rawRevision = value.prompt_revision ?? value.promptRevision
  if (rawRevision === null || rawRevision === undefined || rawRevision === '') return null
  const revision = Number(rawRevision)
  return Number.isSafeInteger(revision) && revision >= 0 ? revision : null
}

export function promptStateFromSnapshot(
  snapshot: unknown,
  buildDraftKey: (promptType: string, payload: Record<string, unknown>) => string,
): BootstrapPromptState | null {
  if (!snapshot || typeof snapshot !== 'object') return null
  const value = snapshot as Record<string, unknown>
  if (!Boolean(value.pending)) return null
  const promptId = String(value.prompt_id ?? value.promptId ?? '').trim()
  const promptType = String(value.prompt_type ?? value.promptType ?? '').trim()
  const rawPayload = value.payload && typeof value.payload === 'object'
    ? value.payload as Record<string, unknown>
    : {}
  const payload = promptPayloadWithMetadata(value, rawPayload)
  if (!promptId || !promptType) return null
  return {
    id: promptId,
    promptType,
    payload,
    draftKey: buildDraftKey(promptType, payload),
  }
}

export function promptSyncUpdateFromSnapshot(
  snapshot: unknown,
  source: PromptSyncSource,
  buildDraftKey: (promptType: string, payload: Record<string, unknown>) => string,
): PromptSyncUpdate | null {
  if (!snapshot || typeof snapshot !== 'object') return null
  const value = snapshot as Record<string, unknown>
  if (typeof value.pending !== 'boolean') return null
  const revision = promptRevisionFromPayload(value)
  if (!value.pending) return { prompt: null, revision, source }
  const prompt = promptStateFromSnapshot(value, buildDraftKey)
  if (!prompt) return null
  return { prompt, revision, source }
}

export function promptSyncUpdateFromRequest(
  payload: Record<string, unknown>,
  buildDraftKey: (promptType: string, payload: Record<string, unknown>) => string,
): PromptSyncUpdate | null {
  const promptId = String(payload.id ?? payload.prompt_id ?? payload.promptId ?? '').trim()
  const promptType = String(payload.prompt_type ?? payload.promptType ?? 'text').trim()
  if (!promptId || !promptType) return null
  const normalizedPayload = promptPayloadWithMetadata(payload, payload)
  return {
    prompt: {
      id: promptId,
      promptType,
      payload: normalizedPayload,
      draftKey: buildDraftKey(promptType, normalizedPayload),
    },
    revision: promptRevisionFromPayload(payload),
    source: 'live',
  }
}

export function reconcilePromptSyncState(
  current: PromptSyncState,
  update: PromptSyncUpdate,
): PromptSyncTransition {
  const nextSignature = promptSignature(update.prompt)

  if (update.revision !== null) {
    if (current.revision !== null && update.revision < current.revision) {
      return { accepted: false, state: current }
    }
    if (current.revision !== null && update.revision === current.revision && nextSignature !== current.signature) {
      return { accepted: false, state: current }
    }
    if (update.source === 'bootstrap' && current.revision === null && current.liveUpdateSeen) {
      return { accepted: false, state: current }
    }
    return {
      accepted: true,
      state: {
        prompt: update.prompt,
        revision: update.revision,
        liveUpdateSeen: current.liveUpdateSeen || update.source === 'live',
        signature: nextSignature,
      },
    }
  }

  // A versioned state is authoritative over legacy packets. A fully legacy
  // backend still works, while an old bootstrap response cannot replace a
  // prompt that was delivered live after the bootstrap request started.
  if (current.revision !== null || (update.source === 'bootstrap' && current.liveUpdateSeen)) {
    return { accepted: false, state: current }
  }
  return {
    accepted: true,
    state: {
      prompt: update.prompt,
      revision: null,
      liveUpdateSeen: current.liveUpdateSeen || update.source === 'live',
      signature: nextSignature,
    },
  }
}

export function dismissPromptSyncState(current: PromptSyncState, promptId: string): PromptSyncState {
  if (!current.prompt || current.prompt.id !== promptId) return current
  return {
    ...current,
    prompt: null,
    signature: promptSignature(null),
  }
}
