import { expect, test } from 'bun:test'
import { resolvePromptAwareStatus, resolvePromptResponseTransition } from './promptTransition'

test('clears the submitted prompt when no newer prompt has arrived', () => {
  expect(resolvePromptResponseTransition('prompt_a', null, true)).toEqual({
    clearPrompt: true,
    nextStatus: 'running',
    nextShellFocus: 'content',
  })
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_a', promptType: 'text' }, true)).toEqual({
    clearPrompt: true,
    nextStatus: 'running',
    nextShellFocus: 'content',
  })
})

test('preserves a newer footer prompt that arrives before the submit response returns', () => {
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_b', promptType: 'text' }, true)).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'prompt',
  })
})

test('preserves a newer dialog prompt that arrives before the submit response returns', () => {
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_b', promptType: 'select' }, true)).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'dialog',
  })
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_b', promptType: 'confirm' }, true)).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'dialog',
  })
})

test('keeps the current prompt retryable when backend does not accept the response', () => {
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_a', promptType: 'text' }, false)).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'prompt',
  })
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_a', promptType: 'select' }, false)).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'dialog',
  })
})

test('keeps a newer prompt when an older response is rejected', () => {
  expect(resolvePromptResponseTransition('prompt_a', { id: 'prompt_b', promptType: 'confirm' }, false)).toEqual({
    clearPrompt: false,
    nextStatus: 'awaiting-input',
    nextShellFocus: 'dialog',
  })
})

test('active prompt wins over non-terminal stage and snapshot status', () => {
  expect(resolvePromptAwareStatus('running', true)).toBe('awaiting-input')
  expect(resolvePromptAwareStatus('ready', true)).toBe('awaiting-input')
  expect(resolvePromptAwareStatus('', true)).toBe('awaiting-input')
  expect(resolvePromptAwareStatus('running', false)).toBe('running')
  expect(resolvePromptAwareStatus('failed', true)).toBe('failed')
  expect(resolvePromptAwareStatus('completed', true)).toBe('completed')
})
