import { expect, test } from 'bun:test'
import { resolvePromptResponseTransition } from './promptTransition'
import type { PromptSnapshot } from './types'

function prompt(promptId: string): PromptSnapshot {
  return { pending: true, promptId, promptType: 'select', payload: {} }
}

test('rejected prompt stays visible', () => {
  expect(resolvePromptResponseTransition('prompt-a', prompt('prompt-a'), false)).toEqual({ clearPrompt: false, accepted: false })
})

test('late response from prompt A cannot clear prompt B', () => {
  expect(resolvePromptResponseTransition('prompt-a', prompt('prompt-b'), true)).toEqual({ clearPrompt: false, accepted: true })
})

test('accepted current prompt can be cleared', () => {
  expect(resolvePromptResponseTransition('prompt-a', prompt('prompt-a'), true)).toEqual({ clearPrompt: true, accepted: true })
})
