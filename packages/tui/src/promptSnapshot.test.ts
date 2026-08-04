import { describe, expect, it } from 'bun:test'
import {
  dismissPromptSyncState,
  EMPTY_PROMPT_SYNC_STATE,
  promptRevisionFromPayload,
  promptStateFromSnapshot,
  promptSyncUpdateFromRequest,
  promptSyncUpdateFromSnapshot,
  reconcilePromptSyncState,
  workerOwnedPromptIsReady,
} from './promptSnapshot'

const buildDraftKey = (promptType: string, payload: Record<string, unknown>) =>
  `${promptType}:${String(payload.title ?? '')}`

describe('promptStateFromSnapshot', () => {
  it('keeps worker-owned HITL hidden until the owning READY revision is visible', () => {
    const prompt = promptStateFromSnapshot(
      {
        pending: true,
        prompt_id: 'prompt_ready_gate',
        prompt_type: 'multiline',
        payload: {
          ready_for_human: true,
          owner_session_name: '分析师-天异星',
          owner_turn_id: 'requirements_clarification_1',
          owner_state_revision: 9,
        },
      },
      buildDraftKey,
    )
    expect(workerOwnedPromptIsReady(prompt, [{
      sessionName: '分析师-天异星', stateRevision: 8, agentState: 'BUSY', turnState: 'succeeded',
      currentTaskRuntimeStatus: 'done', currentTurnId: 'requirements_clarification_1',
    }])).toBe(false)
    expect(workerOwnedPromptIsReady(prompt, [{
      sessionName: '分析师-天异星', stateRevision: 9, agentState: 'READY', turnState: 'succeeded',
      currentTaskRuntimeStatus: 'done', currentTurnId: 'requirements_clarification_1',
    }])).toBe(true)
  })

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

  it('promotes optional Grill metadata from a prompt snapshot without changing legacy payloads', () => {
    const restored = promptStateFromSnapshot(
      {
        pending: true,
        prompt_id: 'grill_1',
        prompt_type: 'select',
        interaction_kind: 'grill',
        question_index: 2,
        owner_runner_id: 'runner-a03',
        question_seq: 2,
        recommendation: '选择 A',
        reason_text: '影响数据边界',
        payload: { title: '数据边界如何定义？' },
      },
      buildDraftKey,
    )

    expect(restored?.payload).toMatchObject({
      title: '数据边界如何定义？',
      interaction_kind: 'grill',
      question_index: 2,
      owner_runner_id: 'runner-a03',
      question_seq: 2,
      recommendation: '选择 A',
      reason_text: '影响数据边界',
    })
  })

  it('treats the outer Grill cursor as authoritative over stale nested metadata', () => {
    const restored = promptStateFromSnapshot(
      {
        pending: true,
        prompt_id: 'grill_cursor',
        prompt_type: 'select',
        owner_runner_id: 'runner-current',
        question_seq: 8,
        payload: {
          interaction_kind: 'grill',
          owner_runner_id: 'runner-stale',
          ownerRunnerId: 'runner-camel-stale',
          question_seq: 7,
          questionSeq: 6,
        },
      },
      buildDraftKey,
    )

    expect(restored?.payload.owner_runner_id).toBe('runner-current')
    expect(restored?.payload.question_seq).toBe(8)
    expect(restored?.payload.ownerRunnerId).toBeUndefined()
    expect(restored?.payload.questionSeq).toBeUndefined()
  })

  it('restores a submittable synthetic Grill multiline prompt with its persistence cursor', () => {
    const restored = promptStateFromSnapshot(
      {
        pending: true,
        prompt_id: 'grill_recovery_session-a_3_abc',
        prompt_type: 'multiline',
        owner_runner_id: 'grill-session:session-a',
        question_seq: 3,
        payload: {
          interaction_kind: 'grill',
          synthetic_recovery: true,
          can_submit: true,
          default_value: '方案 B',
          answer_options: ['方案 A', '方案 B'],
          grill_session_id: 'session-a',
          grill_question_hash: 'sha256:abc',
        },
      },
      buildDraftKey,
    )

    expect(restored?.promptType).toBe('multiline')
    expect(restored?.payload).toMatchObject({
      can_submit: true,
      owner_runner_id: 'grill-session:session-a',
      question_seq: 3,
      grill_session_id: 'session-a',
      grill_question_hash: 'sha256:abc',
    })
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
