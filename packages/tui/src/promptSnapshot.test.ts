import { describe, expect, it } from 'bun:test'
import {
  dismissPromptSyncState,
  EMPTY_PROMPT_SYNC_STATE,
  promptRevisionFromPayload,
  promptStateFromSnapshot,
  promptSyncUpdateFromRequest,
  promptSyncUpdateFromSnapshot,
  reconcilePromptSyncState,
} from './promptSnapshot'

const buildDraftKey = (promptType: string, payload: Record<string, unknown>) =>
  `${promptType}:${String(payload.title ?? '')}`

describe('promptStateFromSnapshot', () => {
  it('restores a pending bootstrap prompt', () => {
    const restored = promptStateFromSnapshot(
      {
        pending: true,
        prompt_id: 'prompt_1',
        prompt_type: 'select',
        payload: {
          title: 'HITL: 开发工程师 需要人工介入',
          is_hitl: true,
        },
      },
      buildDraftKey,
    )

    expect(restored).toEqual({
      id: 'prompt_1',
      promptType: 'select',
      payload: {
        title: 'HITL: 开发工程师 需要人工介入',
        is_hitl: true,
      },
      draftKey: 'select:HITL: 开发工程师 需要人工介入',
    })
  })

  it('ignores non-pending bootstrap prompt snapshots', () => {
    const restored = promptStateFromSnapshot(
      {
        pending: false,
        prompt_id: 'prompt_1',
        prompt_type: 'select',
        payload: { title: 'ignored' },
      },
      () => 'unused',
    )

    expect(restored).toBeNull()
  })

  it('parses optional snake-case and camel-case prompt revisions', () => {
    expect(promptRevisionFromPayload({ prompt_revision: 7 })).toBe(7)
    expect(promptRevisionFromPayload({ promptRevision: '8' })).toBe(8)
    expect(promptRevisionFromPayload({ prompt_revision: -1 })).toBeNull()
    expect(promptRevisionFromPayload({ prompt_revision: 'not-a-number' })).toBeNull()
    expect(promptRevisionFromPayload({})).toBeNull()
  })

  it('rejects an older bootstrap prompt after a newer live prompt', () => {
    const live = promptSyncUpdateFromRequest({
      id: 'prompt_new',
      prompt_type: 'text',
      title: 'new',
      prompt_revision: 5,
    }, buildDraftKey)
    const staleBootstrap = promptSyncUpdateFromSnapshot({
      pending: true,
      prompt_id: 'prompt_old',
      prompt_type: 'select',
      prompt_revision: 4,
      payload: { title: 'old' },
    }, 'bootstrap', buildDraftKey)

    expect(live).not.toBeNull()
    expect(staleBootstrap).not.toBeNull()
    const liveState = reconcilePromptSyncState(EMPTY_PROMPT_SYNC_STATE, live!)
    const staleState = reconcilePromptSyncState(liveState.state, staleBootstrap!)

    expect(liveState.accepted).toBe(true)
    expect(staleState.accepted).toBe(false)
    expect(staleState.state.prompt?.id).toBe('prompt_new')
    expect(staleState.state.revision).toBe(5)
  })

  it('does not let a legacy bootstrap response replace a live legacy request', () => {
    const live = promptSyncUpdateFromRequest({
      id: 'prompt_live',
      prompt_type: 'text',
      title: 'live',
    }, buildDraftKey)
    const staleBootstrap = promptSyncUpdateFromSnapshot({
      pending: true,
      prompt_id: 'prompt_bootstrap',
      prompt_type: 'text',
      payload: { title: 'bootstrap' },
    }, 'bootstrap', buildDraftKey)

    const liveState = reconcilePromptSyncState(EMPTY_PROMPT_SYNC_STATE, live!)
    const staleState = reconcilePromptSyncState(liveState.state, staleBootstrap!)

    expect(staleState.accepted).toBe(false)
    expect(staleState.state.prompt?.id).toBe('prompt_live')
  })

  it('accepts a newer pending=false snapshot and clears the prompt', () => {
    const opened = reconcilePromptSyncState(EMPTY_PROMPT_SYNC_STATE, promptSyncUpdateFromRequest({
      id: 'prompt_1',
      prompt_type: 'select',
      prompt_revision: 10,
    }, buildDraftKey)!)
    const closedUpdate = promptSyncUpdateFromSnapshot({
      pending: false,
      prompt_revision: 11,
    }, 'live', buildDraftKey)
    const closed = reconcilePromptSyncState(opened.state, closedUpdate!)

    expect(closed.accepted).toBe(true)
    expect(closed.state.prompt).toBeNull()
    expect(closed.state.revision).toBe(11)
  })

  it('switches to the next prompt only at a newer revision', () => {
    const first = reconcilePromptSyncState(EMPTY_PROMPT_SYNC_STATE, promptSyncUpdateFromRequest({
      id: 'prompt_a',
      prompt_type: 'text',
      prompt_revision: 20,
    }, buildDraftKey)!)
    const conflictingSameRevision = reconcilePromptSyncState(first.state, promptSyncUpdateFromRequest({
      id: 'prompt_b',
      prompt_type: 'select',
      prompt_revision: 20,
    }, buildDraftKey)!)
    const next = reconcilePromptSyncState(first.state, promptSyncUpdateFromRequest({
      id: 'prompt_b',
      prompt_type: 'select',
      prompt_revision: 21,
    }, buildDraftKey)!)

    expect(conflictingSameRevision.accepted).toBe(false)
    expect(conflictingSameRevision.state.prompt?.id).toBe('prompt_a')
    expect(next.accepted).toBe(true)
    expect(next.state.prompt).toMatchObject({ id: 'prompt_b', promptType: 'select' })
  })

  it('keeps fully legacy live prompt switching and clearing compatible', () => {
    const first = reconcilePromptSyncState(EMPTY_PROMPT_SYNC_STATE, promptSyncUpdateFromRequest({
      id: 'prompt_a',
      prompt_type: 'text',
    }, buildDraftKey)!)
    const next = reconcilePromptSyncState(first.state, promptSyncUpdateFromRequest({
      id: 'prompt_b',
      prompt_type: 'select',
    }, buildDraftKey)!)
    const cleared = reconcilePromptSyncState(next.state, promptSyncUpdateFromSnapshot({
      pending: false,
    }, 'live', buildDraftKey)!)

    expect(next.accepted).toBe(true)
    expect(next.state.prompt?.id).toBe('prompt_b')
    expect(cleared.accepted).toBe(true)
    expect(cleared.state.prompt).toBeNull()
  })

  it('rejects legacy packets after versioned synchronization begins', () => {
    const versioned = reconcilePromptSyncState(EMPTY_PROMPT_SYNC_STATE, promptSyncUpdateFromRequest({
      id: 'prompt_versioned',
      prompt_type: 'text',
      prompt_revision: 2,
    }, buildDraftKey)!)
    const legacy = reconcilePromptSyncState(versioned.state, promptSyncUpdateFromRequest({
      id: 'prompt_legacy',
      prompt_type: 'text',
    }, buildDraftKey)!)

    expect(legacy.accepted).toBe(false)
    expect(legacy.state.prompt?.id).toBe('prompt_versioned')
  })

  it('does not reopen a locally submitted prompt from an equal-revision duplicate', () => {
    const opened = reconcilePromptSyncState(EMPTY_PROMPT_SYNC_STATE, promptSyncUpdateFromRequest({
      id: 'prompt_1',
      prompt_type: 'text',
      prompt_revision: 3,
    }, buildDraftKey)!)
    const dismissed = dismissPromptSyncState(opened.state, 'prompt_1')
    const duplicate = reconcilePromptSyncState(dismissed, promptSyncUpdateFromRequest({
      id: 'prompt_1',
      prompt_type: 'text',
      prompt_revision: 3,
    }, buildDraftKey)!)

    expect(duplicate.accepted).toBe(false)
    expect(duplicate.state.prompt).toBeNull()
  })
})
