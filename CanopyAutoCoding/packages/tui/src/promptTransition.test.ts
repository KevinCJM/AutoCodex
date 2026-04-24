import { expect, test } from 'bun:test'
import { resolvePromptResponseTransition } from './promptTransition'

test('clears the submitted prompt when no newer prompt has arrived', () => {
  expect(resolvePromptResponseTransition('prompt_a', null)).toEqual({
    clearPrompt: true,
    nextStatus: 'running',
    nextShellFocus: 'content',
  })
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_a', promptType: 'text' })).toEqual({
    clearPrompt: true,
    nextStatus: 'running',
    nextShellFocus: 'content',
  })
})

test('preserves a newer footer prompt that arrives before the submit response returns', () => {
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_b', promptType: 'text' })).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'prompt',
  })
})

test('preserves a newer dialog prompt that arrives before the submit response returns', () => {
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_b', promptType: 'select' })).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'dialog',
  })
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_b', promptType: 'confirm' })).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'dialog',
  })
})
